# NSM Fault → Isolation → IRQ + Unified Interrupt/Security Synthesis

This page is the **keystone synthesis** of the Cayman (NC-v3) control-plane fault
architecture. It does two things that no single sibling page does on its own:

1. **The end-to-end NSM PCIe security/error fault chain** — one continuous chain
   from an AXI transaction-integrity violation (or a PCIe link event) → detection
   and sticky latch → the shared host-PCIe **isolation state machine** → the
   **synthetic AXI error-response injection** (`SLVERR`/`DECERR` + `0xDEADBEEF`)
   → the documented **fifo-drain recovery** → the **critical IRQ** to the
   Q7 / management ("Pacific") core.
2. **The unified interrupt + security map** — every fault *source* → *detection*
   → *routing* → *delivery*, fused with the per-FIS **`sprot` enforcement stack**
   (the [`amzn_remapper`/`user_remapper`](../csr/remapper.md) access-control CAM +
   [`qos_prot`](../csr/qos-prot.md) shaper/NTS responder + [`nsm`](../csr/nsm.md)
   integrity watchdog) that *generates* a large share of those faults.

It does **not** re-derive its sources from scratch. Every register, offset, and
reset value is already byte-grounded on a committed sibling; this page reconciles
them into one coherent chain and resolves the cross-page divergences. Each claim
is tagged `[conf · prov]` with `conf ∈ {HIGH, MED, LOW}` and
`prov ∈ {OBSERVED, INFERRED, CARRIED}`. New claims introduced here are grounded
in the shipped Cayman arch-regs artifacts
(`csrs/sprot/{nsm,qos_prot,amzn_remapper,user_remapper}.json`,
`intc/{pcie,peb_intc}_triggers.yaml`) and are marked OBSERVED; reconciliations of
sibling facts are CARRIED.

> **PROVENANCE.** All evidence is derived from static analysis of the shipped,
> RTL-generated **Cayman** arch-regs artifacts (`reg_map`-generated 2023/08/08,
> arch revision `cayman_golden_tapeout_candidate_2_2023_07_21-3-g4723889`) plus
> the firmware-facing intc cause headers (`intc_peb_intc_{enums.h,info.c}`,
> `intc_info_struct.h`) — all RTL-derived descriptor artifacts citeable as
> binary-derived vendor data (lawful interoperability RE). The Cayman generation
> is byte-grounded; **v5 / Maverick is header-OBSERVED only** (see §8), so every
> v5-interior behaviour is flagged INFERRED. The final apex → Q7/GIC vector hop
> is **firmware/HW-owned** and is in no shipped artifact — explicit non-claim
> (§6, §9).

The committed siblings this page synthesizes:
[`../csr/nsm.md`](../csr/nsm.md) (the watchdog),
[`../csr/qos-prot.md`](../csr/qos-prot.md) (the NTS responder + shaper),
[`../csr/remapper.md`](../csr/remapper.md) (the access-control CAM),
[`./errtrig-fis-routing.md`](./errtrig-fis-routing.md) (the errtrig primitive +
L0→L3 cascade),
[`./physical-intc-instances.md`](./physical-intc-instances.md) (the 1,932-instance
physical census),
[`./io-fabric-triggers.md`](./io-fabric-triggers.md) (the io-fabric source map).

---

## 0. Executive summary — the one chain `[chain HIGH]`

The PCIe host path has a **two-layer** fault architecture that meets at the
interrupt controller:

* **LAYER 1 — the security-enforcement stack** (the fault *source*). Every
  privileged fabric master's AXI egress passes through a per-Fabric-Interface-Slice
  (FIS) `sprot` stack of three blocks in series: the **remapper** (access-control
  CAM, decides pass/deny/remap + stamps `AxPROT`), **`qos_prot`** (traffic shaper +
  the NTS no-target-slave responder), and **`nsm`** (the inline AXI integrity
  watchdog). A violation in any of the three *becomes* an interrupt.
* **LAYER 2 — the interrupt fabric** (detection → routing → delivery). 1,688
  Cayman sources (1,560 leaf + 128 apex) latch into 1,932 physical INTC instances
  built from the universal **errtrig PAIR** primitive, summarize through the
  128-input **`peb_intc` apex**, and deliver via MSI-X into the IOFIC/io_fabric
  cascade to the Q7/"Pacific" core.

The NSM fault chain is the canonical Layer-1 → Layer-2 path, in six stages:

| stage | what happens | anchor |
|-------|--------------|--------|
| 1. fault | AXI integrity violation (9 protocol-shape causes), access-control deny, or PCIe link event | `nsm.json wr/rd.status`; `amzn_remapper.json`; `pcie_triggers.yaml` |
| 2. latch | NSM sticky `wr/rd.status` → `control.report`; remapper `addr_denied[57:0]`; NTS `status` FSM | `nsm.json @0x100/0x200`; `amzn_remapper.json @0x60/0x64`; `qos_prot.json nts_amzn.status` |
| 3. isolate | shared host-PCIe isolation SM consumes 6 detection events → `isolation_mode_enter[14]`, debounced by `cfg_isolation_enter.count` | `pcie_triggers.yaml reset_handshake_intr[8..15]` |
| 4. inject | synthetic AXI response to the offending master: `SLVERR`/`DECERR` + 256-bit `0xDEADBEEF` | `nsm.cfg_1.axi_{bresp,rresp}=0x2` + `rd.error_data_0..7=0xdeadbeef` |
| 5. recover | drain AW/W/AR staging FIFOs → reset NTS slices → reset iofabric → clear sticky → re-arm | `nsm.control.reset_staging_fifo`; `qos_prot.nts_isolation.ctrl` |
| 6. deliver | TWO simultaneous paths: summarized `pcie_*_nmi` + DIRECT critical `intr_peb_nsm_axi_timeout` (apex idx111) → Q7 | `peb_intc_triggers.yaml idx111`; `intc_peb_intc_enums.h NSM=111` |

Everything down to and including the apex MSI-X + the firmware cause table is
**OBSERVED**; the apex-pending-bit → Q7/GIC vector hop is **INFERRED** (firmware/
HW-owned). `[chain HIGH; final hop MED/LOW]`

---

## 1. Fault-source taxonomy — what faults, and which block catches it

The host-PCIe fault path has exactly **three** source families. This section
fuses [`../csr/nsm.md`](../csr/nsm.md) §2, [`../csr/remapper.md`](../csr/remapper.md)
§3/§5, and [`./errtrig-fis-routing.md`](./errtrig-fis-routing.md) §2 into one
taxonomy, then verifies the counts.

### 1A. NSM — AXI transaction-integrity violations (the watchdog)

NSM is an **inline per-PCIe-master AXI response watchdog**. It inspects protocol
*shape* and *timing* only — it never checks `AxPROT`, master-ID, VMID, or address
(verified by absence across all 74 bitfields; [`../csr/nsm.md`](../csr/nsm.md)).
Its sticky status enumerates exactly **9** malformed-transaction shapes — 4 write +
5 read, byte-read from `nsm.json`:

| side · reg | bit | field | the malformed shape |
|------------|-----|-------|---------------------|
| `wr.status` @0x100 | 4 | `error_1_b_no_match_aw_ro` | spurious `B` (matches no outstanding `AW`) |
| | 8 | `error_2_b_to_aw_timeout` | `B` response timeout |
| | 12 | `error_3_awvalid_to_awready_timeout` | `AW` handshake stall |
| | 16 | `error_4_wvalid_to_wready_timeout` | `W` handshake stall |
| `rd.status` @0x200 | 0 | `error_0_rlast_before_last_rdata` | short read (`RLAST` early) |
| | 4 | `error_1_rlast_not_set` | long read (`RLAST` never set) |
| | 8 | `error_2_r_no_match_ar` | spurious `R` (matches no outstanding `AR`) |
| | 12 | `error_3_r_to_ar_timeout` | `R` response timeout |
| | 16 | `error_4_arvalid_to_arready_timeout` | `AR` handshake stall |

The read/write asymmetry (4 vs 5) is structurally correct for AXI: beat-count /
`RLAST` integrity only exists on the read side. Outstanding IDs are tracked
per-ID in the `*_pillm` linked-list FIFOs (write 32 IDs / read 128 IDs); a
response mapping to no tracked request is the `no_match`/spurious cause. Two timers
per direction (request-stall + response-latency), each = `cfg_*_tick`
(32-bit prescaler) × `cfg_*_threshold` (12-bit ticks); threshold `0` disables.
All nine roll up into `control.report.{error_wr[0], error_rd[4]}`.

### 1B. Remapper — access-control violations (the deny)

The [`amzn_remapper`](../csr/remapper.md) (privileged, fail-CLOSED) and
`user_remapper` (guest, fail-OPEN) catch the access-control faults via a TCAM on
`{masked 58-bit physical address + optional 10-bit AXI master-ID}` plus parallel
checkers:

* **CAM deny**: `(rd_pass,wr_pass)=0` on a hit, or a fail-closed miss
  (`amzn_cam_pass_on_miss.{rd,wr}=0x0`).
* **`bound_chk`**: a burst spanning the configured `2^N` boundary (default 256 B,
  `granularity` reset `0x8`) → deny.
* **`txn_len_chk`** (Cayman-new): `AWLEN`/`ARLEN` above max (reset `0x7` = 8 beats)
  → deny.
* **`axi_rd/wr_timeout`**: a denied/hung access active past `axi_tout_cnt`.

A deny captures the offending physical address into
`addr_denied_lo[31:0]`@0x60 + `addr_denied_hi[25:0]`@0x64, increments the 48-bit
deny/err/timeout counters, and raises `fis_sprot_intr[0]` (§7).

> **NOTE — the remapper DECIDES, qos_prot RESPONDS.** The remapper schema encodes
> **no** AXI response field (no `read_response`/`write_response`). A denied or
> no-target txn is terminated by the sibling `qos_prot.nts_amzn` (§4); the
> remapper emits **no `0xDEADBEEF` of its own** — it decides pass/deny/remap and
> *delegates* the response. This is the [`errtrig-fis-routing.md`](./errtrig-fis-routing.md)
> §2.5 finding, re-stated here as a coherence keystone:
> **remapper = decide, NTS = respond.** `[HIGH split; MED · INFERRED deny-uses-NTS]`

### 1C. PCIe link events + NTS no-target (the isolation-SM inputs)

The DWC pcie5 x8 controller and the FIS NTS surface the link/no-target events that
drive the isolation SM. Verbatim from `intc/pcie_triggers.yaml`
`reset_handshake_intr` (the **detection** events are
`[8..13]`, the SM **outputs** are `[14]/[15]`):

| idx | trigger name (verbatim) | meaning |
|-----|-------------------------|---------|
| `[8]` | `isolation_sm_linkdown_detected` | PHY/MAC link down |
| `[9]` | `isolation_sm_flr_detected` | Function-Level Reset |
| `[10]` | `isolation_sm_sbr_detected` | Secondary Bus Reset |
| **`[11]`** | **`isolatio_sm_nsm_axi_timeout_detected`** | NSM AXI timeout *(verbatim typo)* |
| `[12]` | `isolation_sm_nts_axi_timeout_detected` | NTS no-target-slave timeout |
| `[13]` | `isolation_sm_pir_detected` | Pacific-Initiated Reset |
| `[14]` | `isolation_sm_isolation_mode_enter` | isolation entered (SM output) |
| `[15]` | `isolation_sm_isolation_mode_exit` | isolation exited (SM output) |

plus two standalone NTS scalars at `pcie_triggers.yaml:1818/1824`:
`nts_iso_r_timeout` ("Isolation NTS R-Channel (Read Data) Timeout") and
`nts_iso_b_timeout` ("Isolation NTS B-Channel (Write Response) Timeout"). All are
`edge_triggered`; `needs_cdc` is absent (already in the INTC domain).

> **QUIRK — the verbatim `isolatio_sm` typo at index `[11]`.** Index `[11]` is
> spelled **`isolatio_sm_nsm_axi_timeout_detected`** — missing the trailing `n` in
> "isolatio". It is the **only** index in `[8..15]` using the misspelled prefix;
> `[8..10]` and `[12..15]` all use the correct `isolation_sm`. A reimplementation
> matching the binary symbol table must reproduce the typo on this one source.
> Confirmed at `pcie_triggers.yaml:61`.

### 1D. SCOPE — host-PCIe ONLY (the negative claim)

The isolation SM is a **host-PCIe** construct. `d2d_triggers.yaml` carries **none**
of `reset_handshake_intr` / `isolation_sm_*` / `nts_iso_*_timeout` (rg "isolation"
= 0 hits; [`./physical-intc-instances.md`](./physical-intc-instances.md), INT-06).
D2D faults on its own per-link ECC/RASDP/wdt set and does **not** present the
linkdown → isolation FSM. §2–§6 below are therefore the host-PCIe path
(`PCIe_A`/`U`/`M` + per-SEngine `PCIe_S`), not a fabric-wide one.

---

## 2. Detection → latch — the sticky state each block holds

Each block latches its own violation state, which the Q7 ISR reads to classify the
fault:

* **NSM** (`nsm.json`): sticky `wr.status`/`rd.status` (RO), OR-rolled into
  `control.report`; cleared by the `PulseOnW` `{wr,rd}.cfg_clear.errors`. Live FSM
  state in `{wr,rd}.sta_state` `[2:0]` where **bits `[1:0]`=`iso_state`,
  bit `[2]`=`short_resp_state`** (verbatim `.h` comment). Boundary telemetry:
  `sta_up_cnt_*` (pcie→NSM), `sta_dn_cnt_*` (NSM→iofabric), `sta_pop_cnt`
  (linked-list pops).
* **Remapper** (`amzn_remapper.json`): `addr_denied_lo[31:0]`@0x60 +
  `addr_denied_hi[25:0]`@0x64 = the `[57:0]` physical address of the most-recent
  DENIED txn (the ISR's violation latch); 48-bit pass/deny/err/timeout counters per
  direction; `delta_mon.error.{aw_minus_b_underflow[0], ar_minus_r_underflow[4]}`
  latch B>AW / R>AR ("more responses than requests") anomalies.
* **NTS** (`qos_prot.json nts_amzn.status` @0x404, RO): a small drain FSM
  `no_target_mode[0]` → `flushing[1]` → `flushed[2]`, with `mode[3]` selecting the
  post-flush state (0=NO-TARGET, 1=BLOCK). Distinct from the PCIe isolation SM —
  this is the NTS responder's *own* drain machine.

---

## 3. The shared host-PCIe isolation state machine `[states HIGH · OBS; arrows MED · INFERRED]`

The isolation SM consumes the six detection events `reset_handshake_intr[8..13]`
and emits `isolation_mode_enter[14]` / `exit[15]`. Entry is **gated** by the NSM's
per-cause enables + a debounce, and runs a **chip-reset quiesce handshake**
(`reset_handshake_intr[0..7]`) on entry.

### 3a. Entry gating — the NSM arming posture

A violation forces isolation only if an enable is set, with a debounce:

* **Per-cause `enter_isolation_mode_on_*` in `{wr,rd}.cfg_1`** — **6 bits total**
  (2 write + 4 read):

  | reg | field | bit | reset |
  |-----|-------|-----|-------|
  | `wr.cfg_1` | `enter_isolation_mode_on_rsp_error` | 2 | `0` |
  | `wr.cfg_1` | `enter_isolation_mode_on_spurious_rsp` | 3 | `1` |
  | `rd.cfg_1` | `enter_isolation_mode_on_rsp_error` | 2 | `0` |
  | `rd.cfg_1` | `enter_isolation_mode_on_long_rsp` | 3 | `1` |
  | `rd.cfg_1` | `enter_isolation_mode_on_short_rsp` | 4 | `1` |
  | `rd.cfg_1` | `enter_isolation_mode_on_spurious_rsp` | 5 | `1` |

  The composite resets are `WR_CFG_1=0x0000000a`, `RD_CFG_1=0x0000003a` — so out of
  reset, **spurious/long/short** auto-isolate but **error-response** isolation is
  disabled.
* **The 5-cause `{wr,rd}.cfg_auto_iso` mask** (`{err_resp, long_resp, short_resp,
  spurious, timeout}`, all reset `0`) — the richer auto-iso path.
* **`{wr,rd}.cfg_isolation_enter.count[15:0]`** — the debounce: "number of cycles
  to count before entering isolation mode; a value of zero disables the counter."
  Reset `0x0` ⇒ immediate entry.

> **GOTCHA — NSM boots BYPASSED and must be armed.** `control.bypass.enable` resets
> to `0x1` (`CONTROL_BYPASS_RESET_VALUE 0x00000001`): the monitor ships
> **bypassed**, and secure firmware must **clear** bit0 to arm it, then program the
> timers and isolation masks. The "bypassed at reset" reading of `bypass=1` is
> name-inferred (the field has no description) — `[MED · INFERRED]` — but the reset
> value is `[HIGH · OBSERVED]`. This mirrors the `qos_prot.csr.control.chicken=1`
> and `nts_isolation.rd_timeout_en=1` boot postures: the whole `sprot` family boots
> transparent/disarmed and is locked up by secure boot.

### 3b. The reset handshake — the chip-reset quiesce protocol

`reset_handshake_intr[0..7]` (verbatim `pcie_triggers.yaml:6..42`) is the
handshake the SM runs on isolation entry, signalling the rest of
the SoC when it is safe to reset:

| idx | name | verbatim meaning |
|-----|------|------------------|
| `[0]` | `handshake_flr_sbr_req_asserted` | FLR/SBR request from host received; PCIe_A entered isolation |
| `[1]` | `handshake_flr_sbr_req_deasserted` | `flr_sbr_done` arrived at PCIe_A |
| `[2]` | `handshake_axi_timeout_asserted` | an AXI txn timed out; PCIe_A entered isolation |
| `[3]` | `handshake_axi_timeout_deasserted` | `axi_timeout_done` arrived |
| `[4]` | `handshake_reset_ready_asserted` | PCIe_A isolated and **ready for the rest of the chip to be reset** |
| `[5]` | `handshake_reset_ready_deasserted` | `reset_req` falling edge seen |
| `[6]` | `handshake_reset_done_ack_asserted` | PCIe_A received `reset_done` |
| `[7]` | `handshake_reset_done_ack_deasserted` | `reset_done` falling edge seen |

### 3c. The state diagram `[state set HIGH · OBS; transitions MED · INFERRED]`

The **states, events, the debounce, the FIFO-drain recovery order, and the NTS
flush FSM are all OBSERVED verbatim**; the **transition arrows** (debounce-cancel,
the exact stop→inject→drain ordering, `bypass` auto-clear on exit) are INFERRED
from field semantics + the recovery note. `iso_state` is only `[1:0]` (≤4 states),
consistent with the encoding below.

```text
 NORMAL ──event── DEBOUNCE ──expire── ISOLATED ──reset_ready[4]── RESET/DRAIN ──done── EXIT ── NORMAL

 ┌────────────────────────────────────────────────────────────────────────────┐
 │ NORMAL  (isolation_mode=0; sta_state.iso_state=0)                            │
 │   masters flow pcie→NSM→iofabric; NSM watches, remapper gates, NTS idle.     │
 └──────────────┬──────────────────────────────────────────────────────────────┘
                │ a detection event fires (reset_handshake_intr[8..13]):
                │   linkdown[8] | FLR[9] | SBR[10] | NSM-tout[11] | NTS-tout[12] | PIR[13]
                │ AND (for NSM-sourced) an enter_isolation_mode_on_* / cfg_auto_iso bit set
                ▼
 ┌────────────────────────────────────────────────────────────────────────────┐
 │ DEBOUNCE  (count down cfg_isolation_enter.count; 0 = immediate)              │
 │   grace window; if the cause clears before expiry, return to NORMAL [MED].   │
 └──────────────┬──────────────────────────────────────────────────────────────┘
                │ debounce expires
                ▼
 ┌────────────────────────────────────────────────────────────────────────────┐
 │ ISOLATED  (isolation_mode_enter[14]; sta_state.iso_state ≠ 0)                │
 │   • stop accepting new master txns                                          │
 │   • NSM injects SLVERR/DECERR + 0xDEADBEEF×8 to in-flight master txns (§4)   │
 │   • NTS: no_target_mode → flushing → flushed (drains outstanding) (§2/§4)    │
 │   • handshake_flr_sbr_req_asserted[0] / handshake_axi_timeout_asserted[2]    │
 └──────────────┬──────────────────────────────────────────────────────────────┘
                │ SM asserts reset_ready[4]; firmware performs RECOVERY (§5):
                │   NSM reset_staging_fifo.{aw,w,ar} drain → nts_isolation slice
                │   resets → reset iofabric
                ▼
 ┌────────────────────────────────────────────────────────────────────────────┐
 │ RESET / DRAIN  (reset_done_ack handshake [6]/[7];                            │
 │   apb_outstding_flushed set→clr at apex idx109/110)                          │
 └──────────────┬──────────────────────────────────────────────────────────────┘
                │ reset complete; firmware re-arms (cfg_clear, re-program enables)
                ▼
 ┌────────────────────────────────────────────────────────────────────────────┐
 │ EXIT → NORMAL  (isolation_mode_exit[15]; iso_state back to 0)                │
 └────────────────────────────────────────────────────────────────────────────┘
```

### 3d. The isolation FSM in C `[structure HIGH; arrows MED · INFERRED]`

This models the SM a reimplementer would drive. The **register names/offsets are
OBSERVED**; the transition *logic* (debounce-cancel, stop→inject→drain order) is
INFERRED from the field descriptions + the recovery note and is flagged inline.

```c
/* NSM isolation FSM — Cayman host-PCIe shared isolation state machine.
 * Register offsets byte-exact from csrs/sprot/nsm.json; the transition arrows
 * are INFERRED from field semantics + the documented recovery note (MED). */

typedef enum { ISO_NORMAL, ISO_DEBOUNCE, ISO_ISOLATED, ISO_RESET_DRAIN, ISO_EXIT } iso_phase_t;

/* The six detection events = reset_handshake_intr[8..13] (pcie_triggers.yaml). */
typedef enum {
    DET_LINKDOWN   = 8,   /* isolation_sm_linkdown_detected            */
    DET_FLR        = 9,   /* isolation_sm_flr_detected                 */
    DET_SBR        = 10,  /* isolation_sm_sbr_detected                 */
    DET_NSM_TOUT   = 11,  /* isolatio_sm_nsm_axi_timeout_detected (sic)*/
    DET_NTS_TOUT   = 12,  /* isolation_sm_nts_axi_timeout_detected     */
    DET_PIR        = 13,  /* isolation_sm_pir_detected                 */
} iso_detect_t;

typedef struct {
    iso_phase_t phase;
    uint16_t    debounce_left;          /* loaded from {wr,rd}.cfg_isolation_enter.count[15:0] */
    bool        cause_still_active;     /* HW input; MED-INFERRED cancel path                  */
} iso_sm_t;

/* Whether an NSM-sourced violation is *armed* to isolate (cfg_1 per-cause OR cfg_auto_iso).
 * For non-NSM events (linkdown/FLR/SBR/NTS/PIR) the SM enters unconditionally. */
static bool nsm_arm_isolation(NsmDir *d, iso_detect_t ev) {
    if (ev != DET_NSM_TOUT) return true;                 /* link/reset events always isolate */
    /* cfg_1.enter_isolation_mode_on_* OR cfg_auto_iso.* gates the NSM-sourced path. */
    return d->cfg_1.enter_iso_on_rsp_error  || d->cfg_1.enter_iso_on_spurious_rsp
        || d->cfg_1.enter_iso_on_long_rsp   || d->cfg_1.enter_iso_on_short_rsp
        || (d->cfg_auto_iso.raw & 0x1f);                 /* err/long/short/spurious/timeout */
}

/* Advance the SM one tick. NORMAL→DEBOUNCE→ISOLATED→RESET_DRAIN→EXIT→NORMAL. */
void iso_sm_tick(iso_sm_t *sm, NsmDir *d, iso_detect_t ev, bool event_fired) {
    switch (sm->phase) {
    case ISO_NORMAL:
        if (event_fired && nsm_arm_isolation(d, ev)) {
            /* cfg_isolation_enter.count == 0 ⇒ immediate isolation (debounce disabled). */
            sm->debounce_left = nsm_iso_enter_count(d);  /* {wr,rd}.cfg_isolation_enter.count */
            sm->phase = (sm->debounce_left == 0) ? ISO_ISOLATED : ISO_DEBOUNCE;
        }
        break;
    case ISO_DEBOUNCE:
        /* MED-INFERRED: if the cause clears before expiry, return to NORMAL. */
        if (!sm->cause_still_active)       sm->phase = ISO_NORMAL;
        else if (--sm->debounce_left == 0) sm->phase = ISO_ISOLATED;
        break;
    case ISO_ISOLATED:
        /* HW: assert isolation_mode_enter[14]; stop accepting new txns;
         * NSM injects the synthetic error response (§4); NTS drains (no_target→
         * flushing→flushed). When quiesced, the SM asserts reset_ready[4]. */
        if (iso_quiesced(d))              sm->phase = ISO_RESET_DRAIN;
        break;
    case ISO_RESET_DRAIN:
        /* Firmware drives the §5 recovery; HW tracks apb_outstding_flushed
         * (apex idx110 set → idx109 clr) and the reset_done_ack handshake [6]/[7]. */
        if (reset_done_acked(d))          sm->phase = ISO_EXIT;
        break;
    case ISO_EXIT:
        /* isolation_mode_exit[15] fires; iso_state → 0. */
        sm->phase = ISO_NORMAL;
        break;
    }
}
```

---

## 4. Synthetic error-response injection — what the master gets back

When a txn is aborted, a synthetic AXI response is returned so the offending master
unblocks rather than wedging the on-die AXI fabric. **Two injectors, identical
poison convention, different trigger** — this is the **NTS terminator family**
([`errtrig-fis-routing.md`](./errtrig-fis-routing.md) §2.5):

* **(A) NSM** (misbehaving-but-present master): `BRESP = wr.cfg_1.axi_bresp`
  (`@0x118`, reset `0x2` = SLVERR; `0x3` = DECERR), `RRESP = rd.cfg_1.axi_rresp`
  (`@0x218`, reset `0x2`), plus a **256-bit** `RDATA` from `rd.error_data_0..7`
  (`@0x21c..0x238`, each reset `0xdeadbeef` — verbatim "dummy return AXI_RDATA
  after error") — **exactly 8× `0xdeadbeef`** in `nsm.json`.
* **(B) NTS** (`qos_prot.nts_amzn`, absent target during isolation/power-down):
  `read_response`@0x408 / `write_response`@0x410 (reset `0x2` SLVERR; `0x3` DECERR;
  `0x0` OK), `read_data`@0x40c (reset `0xdeadbeef`, "replicated across the bus").
  **Exactly ONE** materialized `read_data` register in
  `qos_prot.json` holds the poison word (`rg -ci deadbeef qos_prot.json` = 2, but
  the second hit is that register's own **Description** text "default=deadbeef" @line
  1820, *not* a second register). `control.mode` selects NO-TARGET vs BLOCK
  post-flush.

The **remapper deny reuses (B)** — it has no own response field. So three blocks
terminate AXI faults with the **same signature `SLVERR(0x2)` + `0xDEADBEEF`**:
`qos_prot` NTS, `amzn`/`user_remapper` (deny, via NTS), and `nsm`. The poison
matches the `udma_gen_ex` DEADBEEF convention.

> **CORRECTION — the deadbeef *register* census is 8 / 1 / 0, not 8 / 2 / 0.** A
> naïve reading equates the two injectors, but the schemas differ — and an earlier
> pass over-counted the qos side. NSM holds **8** `error_data_*` registers (a full
> 256-bit beat materialized in CSR @`0x21c..0x238`), while `qos_prot`-NTS holds
> **exactly ONE** materialized register (`nts_amzn.read_data` @abs `0x40c`) and
> **replicates** that single `0xDEADBEEF` word across the 256-bit beat at response
> time. The "2" previously cited for `qos_prot` was **count-grep inflation**:
> `rg -ci deadbeef qos_prot.json` = 2, but the second hit is the register's own
> Description field ("default=deadbeef"), not a second register. Both blocks put
> `DEADBEEF DEADBEEF …` on the wire; only the CSR materialization differs (8-reg vs
> 1-reg-replicated). The remapper holds **zero** — it delegates to NTS. Census =
> **8 / 1 / 0** (nsm / qos_prot-NTS / remapper). When you see `0xDEADBEEF` on the
> read-data bus, the master hit one of these three FIS guards.

---

## 5. Recovery — the documented fifo-drain teardown order `[HIGH · OBSERVED order]`

The shipped field descriptions name the teardown sequence explicitly. NSM
`control.reset_staging_fifo` (verbatim) and the NTS
`nts_isolation.ctrl` together define it:

**NSM staging-FIFO drain** (`reset_staging_fifo` @0x008):

| bit | field | special | verbatim description |
|-----|-------|---------|----------------------|
| 0 | `aw` | **PulseOnW** | "clear aw bus staging fifo. This reset should be done after entering isolation mode, before resetting the iofabric" |
| 1 | `w` | None | "clear w bus staging fifo. … after entering isolation mode, before resetting the iofabric" |
| 2 | `ar` | None | "clear ar bus staging fifo. … after entering isolation mode, before resetting the iofabric" |

> **CORRECTION — `PulseOnW` asymmetry on `reset_staging_fifo`.** Only bit `[0]` `aw`
> carries `SpecialAccess=PulseOnW`; `w[1]` and `ar[2]` show `None`. All three are
> semantically pulse-clear ("clear … staging fifo"), so the `None` on `w`/`ar` is
> most likely a schema inconsistency, **not** a real RW-latch. A faithful rebuild
> should model `aw` as a self-clearing pulse and treat `w`/`ar` per the literal
> `None` unless silicon proves otherwise. `[MED · OBSERVED schema · INFERRED intent]`

**NTS-side recovery** (`qos_prot.nts_isolation.ctrl` @0x600, Cayman-new):

| bit | field | reset | role |
|-----|-------|-------|------|
| 0 | `rd_reset` | `0` | reset NTS read pending + outstanding counts |
| 1 | `wr_reset` | `0` | reset NTS write pending + outstanding counts |
| 2 | `rd_timeout_en` | `1` | arm NTS read timeout (boots **enabled**) |
| 3 | `wr_timeout_en` | `0` | arm NTS write timeout |
| 4 | `slv_slice_reset` | `0` | reset AXI slices **between fabric and NTS** |
| 5 | `mstr_slice_reset` | `0` | reset AXI slices **between NTS and the block** |

**The documented order:** enter isolation → NSM drain `{aw,w,ar}` staging FIFOs +
NTS slice/count resets → reset iofabric (via the reset_handshake `reset_ready[4]`/
`reset_done_ack[6]` handshake) → clear NSM sticky via `{wr,rd}.cfg_clear.errors` +
remapper `clear_stats` → re-arm → `isolation_mode_exit[15]`. The apex
`apb_outstding_flushed_{set,clr}` (idx110/109, critical:1) gate the "wait until all
outstanding flushed, then trigger" quiesce handshake used during the drain — the
fault and the drain handshake are **co-located** fast-path apex bits
(idx109/110/111). `[HIGH order; MED exact interleave]`

### Recovery in C `[structure HIGH; ordering OBSERVED from field descriptions]`

```c
/* NSM/NTS fifo-drain recovery — the documented teardown order.
 * Register names/offsets byte-exact from nsm.json / qos_prot.json. The STEP ORDER
 * is OBSERVED verbatim ("after entering isolation, before resetting the iofabric");
 * the cross-block interleave granularity is MED-INFERRED. */
void nsm_isolation_recover(NsmDir *wr, NsmDir *rd, NtsCtl *nts, IoFabric *iof) {
    /* Precondition: we are in ISOLATED (isolation_mode_enter asserted) and the SM
     * has asserted reset_ready[4]. The masters are already quiesced and any
     * in-flight txn has been answered with SLVERR/DECERR + 0xDEADBEEF (§4). */

    /* 1) Drain the AW/W/AR staging FIFOs — AFTER isolation, BEFORE iofabric reset. */
    nsm_pulse(&wr->control.reset_staging_fifo, RSF_AW);   /* bit0, PulseOnW            */
    nsm_pulse(&wr->control.reset_staging_fifo, RSF_W);    /* bit1, None (intent: pulse)*/
    nsm_pulse(&rd->control.reset_staging_fifo, RSF_AR);   /* bit2, None (intent: pulse)*/

    /* 2) Reset the NTS pending/outstanding counters and the AXI register-slices on
     *    both sides of the NTS, so a stuck master is isolated without a block reset. */
    nts->ctrl.rd_reset = 1;  nts->ctrl.wr_reset = 1;      /* drain counters            */
    nts->ctrl.slv_slice_reset  = 1;                       /* fabric↔NTS slices         */
    nts->ctrl.mstr_slice_reset = 1;                       /* NTS↔block slices          */

    /* 3) Reset the iofabric via the chip-reset handshake. HW tracks the quiesce with
     *    apb_outstding_flushed (apex idx110 set → idx109 clr) and reset_done_ack[6]. */
    iofabric_reset_via_handshake(iof);                    /* reset_ready[4] → reset_done_ack[6] */

    /* 4) Clear sticky violation state so the next fault re-latches cleanly. */
    nsm_pulse(&wr->cfg_clear, CLEAR_ERRORS);              /* {wr,rd}.cfg_clear.errors  */
    nsm_pulse(&rd->cfg_clear, CLEAR_ERRORS);
    /* remapper clear_stats (PulseOnW) clears the deny/err counters — sibling block.  */

    /* 5) Re-arm: re-program the per-cause enables / cfg_auto_iso, optionally re-assert
     *    bypass.enable, then the SM asserts isolation_mode_exit[15] → iso_state = 0. */
}
```

---

## 6. The unified interrupt + security map — source → detection → routing → delivery

This is the second half of the synthesis: every fault *source* mapped to its
*detector*, *router*, and *delivery* path, and the per-FIS `sprot` enforcement
stack that *generates* much of the traffic. It fuses
[`./errtrig-fis-routing.md`](./errtrig-fis-routing.md),
[`./physical-intc-instances.md`](./physical-intc-instances.md), and
[`./io-fabric-triggers.md`](./io-fabric-triggers.md).

### 6a. The per-FIS sprot enforcement stack (the source)

Every privileged master's AXI egress passes through a FIS `sprot` container in
series: **remapper FIRST** (`..._SPROT_AMZN_REMAPPER` @+0x0000, `0x1000`),
**`qos_prot` SECOND** (`..._SPROT_QOS` @+0x1000, `0x1000`), filling the `0x2000`
`FIS_0_SPROT`. The NSM is a *separate* `PEB_APB_IO` leaf (28 real instances = 2 PEB
× 14 PCIe interfaces), inline on each PCIe master's AXI — schema-dir `sprot` but
**not** inside the `FIS_0_SPROT` container.

The trust-boundary invariant (the central security primitive, FROZEN across
Sunda/Cayman/Mariana):

| block · field | `amzn` (privileged) | `user` (guest) |
|---------------|---------------------|----------------|
| `*_cam_pass_on_miss.{rd,wr}` reset | **`0x0`** = fail-CLOSED (whitelist) | **`0x1`** = fail-OPEN (allow) |
| `master_prot.{arprot,awprot}` | **`0x2`** (non-secure privileged data) — present | **absent** — guest cannot emit `AxPROT` |
| `user_cam_ctl` wipe / `*_byp_id` override | YES (reaches into guest CAM) | n/a |

> **WALL — invert either `pass_on_miss` reset and you break the chip.** `amzn`
> fail-open removes the firmware whitelist entirely; `user` fail-closed deadlocks
> every un-provisioned guest master. Confirmed byte-exact
> (`amzn rd/wr=0x0`, `user rd/wr=0x1`). The guest is *contained* by the surrounding
> privileged firewall + the NTS error path, not by its own default-deny.

### 6b. The errtrig routing fabric (the router) `[HIGH · CARRIED]`

Every per-block aggregation is an **errtrig PAIR** = two `intc_4grp` units
(`TRIG_0`+`TRIG_1` = 256-cap) + a `notific_1_queue`, in a `0x3000` container. The
flavor tracks **privilege, not domain**: USER → `intc_4grp_msix` (856 instances,
MSI-X straight to the PCIe host); AMZN → `intc_4grp_no_msix` (1,070 instances,
emits 4 severity wire-ORs `{Error,Abort,Fatal,Log}` + `Mask_msi_x` summary = one
`nmi_out` wire). 1,932 total physical INTC instances. The Cayman **errtrig PAIR
count is 962** (428 USER + 534 AMZN) — corrected from the earlier "642" in
[`./errtrig-fis-routing.md`](./errtrig-fis-routing.md) §1.1.

Cause-register semantics (the Annapurna convention): `int_cause_grp` is **W0C**
(write-0-to-clear, HW-set-wins on collision — *not* the ARM GIC W1C habit);
`int_cause_set_grp` is **W1S**; `int_mask_grp` resets `0xffffffff` (all masked at
reset, safe). `[HIGH · CARRIED from errtrig-fis-routing.md / physical-intc-instances.md]`

### 6c. The 128-input apex + the 32 critical fast-path (the delivery)

The `peb_intc` apex is a **flavor pair** `{PEB_INTC_TRIG_0 (no_msix, 128-input
summary) + PEB_INTC_MSIX (host-delivery twin)}`, replicated per PEB (×2). The 128
inputs = 96 leaf `nmi_out` summaries + **32** direct PEB-local critical sources
(exactly **32** `critical: 1` in
`peb_intc_triggers.yaml`). `critical:1` = "*not a summary*" (verbatim
`intc_info_struct.h`) — an event firmware reacts to without decoding a per-domain
summary register.

The fault-and-recovery cluster of the NSM chain sits at fixed, **co-located** apex
indices (file order = bit order; `apb_outstding_flushed`
at trigger-positions 110/111 = idx109/110, NSM at 112 = idx111):

| apex input | idx | critical | edge/level | role |
|------------|-----|----------|------------|------|
| `apb_outstding_flushed_clr_triggers_out` | 109 | 1 | LEVEL | isolation/recovery drain |
| `apb_outstding_flushed_set_triggers_out` | 110 | 1 | LEVEL | isolation/recovery drain |
| `intr_peb_nsm_axi_timeout` | **111** | 1 | **LEVEL** | **NSM security fast-path** |
| `fis_sprot_intr[0..4]` | 118–122 | — | edge | FIS sprot (remapper deny / delta / tmu / qos) |
| `fis_cntrl_intr[0..4]` | 123–127 | — | edge | FIS EP posted-write SLVERR |

Confirmed verbatim from `peb_intc_triggers.yaml`: `intr_peb_nsm_axi_timeout`,
`edge_triggered: false` (LEVEL), `nmi_mask: 0`, `nmi_msix_mask: 0`, `critical: 1`,
description "PEB SPROT NSM timeout or error interrupt".

### 6d. The NSM TWO-PATH delivery (the meeting point)

The NSM AXI timeout reaches Pacific by **two simultaneous paths**:

* **(a) SUMMARIZED**: NSM violation → `reset_handshake_intr[11]
  isolatio_sm_nsm_axi_timeout_detected` → `isolation_mode_enter[14]` → the PCIe-leaf
  AMZN `u_amzn_errtrig.nmi_out` → `pcie_m0_nmi`(idx0)/`pcie_a0_nmi`(idx1)/
  `pcie_se{0,1}_b_combined_nmi`(idx5/6) apex summary. The isolation SM has **no
  dedicated apex bit** of its own. `[HIGH · OBSERVED; fan-in MED · INFERRED]`
* **(b) DIRECT**: NSM violation → `intr_peb_nsm_axi_timeout`, the DIRECT critical:1
  LEVEL apex bit (idx111). This is the security fast-path.

The other isolation-SM sources (`reset_handshake_intr[8..15]`) live one level
*down* in `pcie_triggers.yaml`, **not** in the apex — they summarize via the PCIe
`nmi` bits. NSM is the one exception with a direct critical bit.

### 6e. The firmware-response tie — the Q7/"Pacific" ISR dispatch table `[HIGH · OBSERVED tables; MED · INFERRED logic]`

The shipped firmware-facing headers *are* the Q7-side binding (the cause tables the
management-core ISR indexes):

* `intc_peb_intc_enums.h`:
  `aws_intc_critical_cause_peb_intc_intr_peb_nsm_axi_timeout = 111` (MATCHES the
  apex idx); `apb_outstding_flushed_{clr=109,set=110}`; `fis_sprot_intr_{0..4} =
  118..122`. (33 critical_cause entries total = the 32 critical:1 + convention.)
* `intc_peb_intc_info.c`: `const struct intc_info aws_intc_info_peb_intc[…]`,
  "indexed by the cause enumeration (= the bit index within the INTC)"; NSM entry
  `.index=111, .edge_triggered=false, .critical=true`.
* `intc_info_struct.h`: `index` ("which of the 256 INTC bits"), `nmi_mask` ("allows
  PACIFIC to specify … what is masked"), `critical` ("true if … *not a summary* in
  PEB_INTC"). **"Pacific" = the Q7/management core.**

FW model (structure OBSERVED; handler logic INFERRED): apex MSI-X raises → Q7 reads
the apex pending image → for each set bit, index `aws_intc_info_peb_intc[bit]`;
`.critical=true` (NSM idx111) → fast-path NSM/isolation handler **without** decoding
a summary; `.critical=false` → descend into the named leaf summary. The NSM handler
reads `sta_state.iso_state` + `wr/rd.status` to classify, reads remapper
`addr_denied_lo/hi` on a deny, drives the §5 recovery, clears sticky, acks
`reset_done`. **No ISR C body ships** in this repo — only the cause/info tables +
register schemas. The multi-gen enum binding is stable (Sunda NSM=96 due to its
97-wide apex; Cayman/Mariana NSM=111).

### 6f. The consolidated architecture diagram `[structure HIGH; L3 hop MED/LOW]`

```text
 ============================ LAYER 1: SECURITY (the source) ============================
  PCIe master AXI txn   (pcie→NSM boundary; nsm.sta_up_cnt_* count it)
       │   (per-FIS sprot stack, in series at the master egress edge)
       ▼
  ┌─ amzn/user_remapper CAM ─┐  ┌─ qos_prot (NTS) ─┐  ┌─ nsm watchdog ─┐
  │ masked-addr[57:0]+AXI-ID │  │ shaper (chicken=1 │  │ track out. IDs │
  │ → rd/wr_pass DENY        │  │  transparent boot) │  │  (pillm FIFO)  │
  │ bound/burst-len/AXI-tout │  │ NTS: SLVERR/DECERR │  │ spurious/RLAST/│
  │ AxPROT gen (amzn 0x2)    │  │  + 0xDEADBEEF when │  │ handshake|rsp  │
  │ amzn FAIL-CLOSED (0x0)   │  │  no target/flushed │  │ timeout (9)    │
  │ user FAIL-OPEN  (0x1)    │  └────────┬───────────┘  └───────┬────────┘
  └─────────┬────────────────┘           │                      │
            │ deny                        │ no-target            │ violation
            ▼                             ▼                      ▼
   addr_denied[57:0]            no_target→flushing→flushed   (a) sticky wr/rd.status
   + 48-bit deny ctrs          + SLVERR/DECERR+0xDEADBEEF    (b) inject SLVERR/DECERR
            │            (remapper deny REUSES NTS resp) ──►     + 0xDEADBEEF×8
            └── fis_sprot_intr[0..4] (deny/delta/tmu/qos) ──┐   (c) if enter_iso set: debounce
                                                            │            │
                                ┌───── SHARED PCIe ISOLATION SM (host PCIe only) ──────┐
                                │ reset_handshake_intr[8]linkdown [9]FLR [10]SBR        │
                                │ [11]NSM-tout [12]NTS-tout [13]PIR → enter[14]/exit[15]│
                                │ reset handshake [0..7]; RECOVERY (§5): NSM            │
                                │ reset_staging_fifo{aw,w,ar} → nts_isolation slices    │
                                │ → reset iofabric → cfg_clear → re-arm                 │
                                └────────────────┬──────────────────────────────────────┘
 ============================ LAYER 2: INTERRUPT FABRIC (route + deliver) ===============
            │ (a) summarized                    │ (b) DIRECT critical fast-path
            ▼                                    ▼
  PCIe-leaf u_amzn_errtrig.nmi_out      intr_peb_nsm_axi_timeout (apex idx111, LEVEL crit)
  → pcie_{m0,a0}_nmi apex summary               │
            │                                    │
            ▼                                    ▼
  ┌─ peb_intc APEX (128-in: 96 summaries + 32 critical) ─┐
  │ {TRIG_0 no_msix + MSIX twin}, per-PEB                 │
  │ idx109/110 apb-flush + idx111 NSM (direct crit)       │
  └────────────────────────┬─────────────────────────────┘
                           ▼  MSI-X → IOFIC/io_fabric cascade
  ┌─ Q7 / management ("Pacific") core ─────────────────────────────────────────┐
  │ ISR indexes aws_intc_info_peb_intc[bit];                                    │
  │ .critical=true (NSM idx111) → fast-path NSM/iso handler (read sta_state +    │
  │   status, remapper addr_denied; drive §5 recovery; clear sticky; ack)       │
  │ .critical=false → descend into named leaf summary                           │
  │ (apex-pending-bit → Q7/GIC vector map = firmware/HW-owned)         [MED/LOW] │
  └─────────────────────────────────────────────────────────────────────────────┘
```

---

## 7. The fis_sprot vector — the access-control IRQ tie

The remapper deny, delta-monitor underflows, and TMU/qos events surface as the
6-entry `fis_sprot_intr` vector, present in every leaf (verbatim, idx118 = "amzn
remapper denied a transaction"):

| idx | verbatim description | source block |
|-----|----------------------|--------------|
| `[0]` | amzn_remapper denied a transaction | CAM/policy DENY (remapper) |
| `[1]` | delta monitor: R responses > AR request | `delta_mon.ar_minus_r` (remapper) |
| `[2]` | tmu detected an AXI timeout | `axi_rd/wr_timeout` (remapper) |
| `[3]` | delta monitor: B responses > AW request | `delta_mon.aw_minus_b` (remapper) |
| `[4]` | qos pmu interrupt (OR of all 16 PMU ctr IRQs) | `qos_pmu` |
| `[5]` | `fis_sprot_spare_0` | spare |

At the apex only the **5** single-index entries appear (idx118–122, edge, shape-D,
no nmi/critical); the 6th (`fis_sprot_spare_0`) is a leaf-only entry. The leaf
vector is replicated per sprot sub-block — a 2-D `fis_sprot_intr[0..1][0..5]` (12
per leaf in the io-fabric map; [`./io-fabric-triggers.md`](./io-fabric-triggers.md)
§3 GROUP G).

> **CORRECTION — the delta/tmu sources live in `amzn_remapper.json`, not
> `qos_prot.json`.** Per [`./io-fabric-triggers.md`](./io-fabric-triggers.md) §4.5,
> the **`delta_mon`** bundle and the **`axi_rd/wr_timeout`** counters that arm
> `fis_sprot_intr[1]/[2]/[3]` live in the **remapper** schema; `qos_prot.json`
> holds only the trigger *arming* controls (`trigger_on_{bresp,rresp}`,
> `nts_isolation.{rd,wr}_timeout_en`) and the LFSR shaper. Both files are in the
> sprot chain, so the routing is correct, but the **register-family attribution**
> is the remapper.

> **NOTE — there is no separate `user_remapper` deny IRQ.** A guest-CAM deny
> surfaces through the **same** `amzn_remapper`-named `fis_sprot_intr[0]` line, so
> the privileged ISR sees both privileged and guest denials.
> `[HIGH trigger names; MED · INFERRED guest-deny routing]`

---

## 8. Cross-generation `[Cayman HIGH · OBSERVED; v5 header-OBSERVED]`

The enforcement core is **frozen Cayman == Maverick**: the `intc_4grp` errtrig
primitive (the 12-register per-group set, the 4 severity wire-ORs, W0C/W1S) is
byte-structurally identical across Sunda/Cayman/Mariana/Maverick; the 9 NSM causes,
the 8× deadbeef, `axi_bresp/rresp=0x2`, the 6 isolation enables, and the remapper
fail-closed/open + `AxPROT 0x2` are all frozen.

> **NOTE — Maverick (v5) NSM is OBSERVED on disk, not header-only.** The Maverick
> `nsm.json` itself ships
> (`…/arch-headers/maverick/vpc-mirror/arch-regs/src/csrs/sprot/nsm.json`) and is
> byte-identical to Cayman. So the **NSM register map specifically** is
> `[HIGH · OBSERVED]` on v5 — but the v5-**interior** behaviour (the AXI dataflow,
> the apex wiring, the isolation-SM sequencing behind that map) remains
> `[* · INFERRED]`. Cayman is authoritative and representative for all gens.

The deltas (CARRIED): Cayman widens the AXI master-ID 8→10 bit, adds `bypass-ID` +
`txn_len_chk`, adds `qos_prot.nts_isolation` + NTS `mode` + 9-bit block-ID. Mariana
adds AXI parity/protocol-checker bundles on the frozen shaper. **Maverick is a
decentralized, security-hardened rewrite** (header-OBSERVED): 13 per-IP-block
embedded INTCs (Cayman has zero), the `iofic_x8_msix` SWOM-locked security IOFIC,
a per-die apex (119 entries, 79 criticals vs Cayman's 32), and FIS parity sources —
**all v5-interior claims INFERRED**.

---

## 9. Open items / explicit non-claims

* **O1.** The apex-pending-bit → Q7/GIC **vector hop** is firmware/HW-owned and in
  no shipped artifact. The chain is HIGH down to the apex MSI-X + the FW cause
  table; the final hop is MED/LOW (a GIC is confirmed via the CXELA500 ELA "connects
  to a GIC as an IRQ input", but the vector map is not register-encoded).
  `[LOW · INFERRED]`
* **O2.** The firmware **ISR bodies** are not in this repo — only the cause/info
  dispatch tables + register schemas ship. The dispatch table is OBSERVED; the
  handler step sequence (§6e) is INFERRED. `[MED · INFERRED]`
* **O3.** The isolation-SM **transition arrows** (debounce-cancel-on-clear, the
  exact stop→inject→drain ordering, `bypass` auto-clear on exit) are INFERRED from
  field names + the recovery note; the STATES/EVENTS are OBSERVED. `[MED · INFERRED]`
* **O4.** The (a) summarized **fan-in** through `pcie_*_nmi`, and "remapper deny
  reuses the NTS response path", are INFERRED from naming + cross-file
  corroboration. `[MED · INFERRED]`
* **O5.** `reset_staging_fifo.{w,ar}` `SpecialAccess=None` is read as a schema
  inconsistency (intended pulse-clear); model `aw` as PulseOnW and `w`/`ar` per the
  literal `None` unless silicon proves otherwise (§5). `[MED]`
* **O6.** All **v5/Maverick interior** behaviour is header-OBSERVED only (§8); the
  NSM *register map* is the one exception (OBSERVED on disk). `[v5-interior · INFERRED]`

---

## 10. Confidence / provenance ledger

| claim | conf · prov | grounding |
|-------|-------------|-----------------------------------------------------|
| NSM 9 protocol-shape causes (4 wr + 5 rd) | HIGH · OBSERVED | `nsm.json wr/rd.status` |
| `0xDEADBEEF` register census **8 / 1 / 0** (NSM / qos NTS / remapper) | HIGH · OBSERVED | NSM 8× `error_data_*` regs; qos_prot 1 `read_data` reg (the `rg -ci deadbeef`=2 second hit is the Description text, not a 2nd reg); remapper delegates |
| `axi_bresp/rresp` reset `0x2` (SLVERR) | HIGH · OBSERVED | `nsm.json @0x118/0x218` |
| 6 `enter_isolation_mode_on_*` (2 wr + 4 rd) | HIGH · OBSERVED | `nsm.json cfg_1` (counted = 6) |
| `cfg_auto_iso` 5-cause `{err_resp,long,short,spurious,timeout}` | HIGH · OBSERVED | `nsm.json` |
| `bypass.enable` reset `0x1` (boots bypassed) | HIGH value · MED interp | `nsm.json control.bypass` |
| `reset_staging_fifo` aw=PulseOnW, w/ar=None, verbatim "after iso, before iofabric" | HIGH · OBSERVED | `nsm.json @0x008` |
| `reset_handshake_intr[8..15]` + verbatim `isolatio_sm` typo at [11] | HIGH · OBSERVED | `pcie_triggers.yaml:45..80,61` |
| `reset_handshake_intr[0..7]` quiesce handshake descriptions | HIGH · OBSERVED | `pcie_triggers.yaml:6..42` |
| `nts_iso_{r,b}_timeout` scalars | HIGH · OBSERVED | `pcie_triggers.yaml:1818/1824` |
| qos NTS `no_target_mode/read_response/write_response/mode/nts_isolation` | HIGH · OBSERVED | `qos_prot.json` |
| remapper `pass_on_miss` amzn rd/wr=`0x0` (closed) / user=`0x1` (open) | HIGH · OBSERVED | `{amzn,user}_remapper.json` |
| `master_prot` present in amzn (1), absent in user (0) | HIGH · OBSERVED | `rg -c master_prot` |
| apex `critical:1` count = 32; NSM idx111 LEVEL crit; flush idx109/110 | HIGH · OBSERVED | `peb_intc_triggers.yaml` (=32; lines 698/705/712) |
| `fis_sprot_intr` 118–122, `fis_cntrl_intr` 123–127 | HIGH · OBSERVED | `peb_intc_triggers.yaml` |
| FW tie: `intc_peb_intc_enums.h` NSM=111 `.critical=true` | HIGH · OBSERVED | intc cause headers |
| v5 `nsm.json` present on disk, byte-identical to Cayman | HIGH · OBSERVED | `…/maverick/…/csrs/sprot/nsm.json` |
| host-PCIe-only scope (d2d has no isolation SM) | HIGH · CARRIED | INT-06 / d2d_triggers.yaml |
| errtrig PAIR primitive / 1,932 instances / 962 pairs / W0C-W1S | HIGH · CARRIED | errtrig-fis-routing / physical-intc-instances |
| isolation-SM transition arrows; stop→inject→drain order; deny-reuses-NTS; (a) fan-in; FW handler logic | MED · INFERRED | field semantics + recovery note + cross-file |
| apex-pending-bit → Q7/GIC vector hop; ISR bodies | LOW · INFERRED | not register-encoded; firmware/HW-owned |
| all v5/Maverick interior behaviour (except NSM register map) | v5-interior · INFERRED | header-OBSERVED only |

**Nothing fabricated; no vendor source snapshot referenced. All output reads as
derived from binary/static analysis of the shipped Cayman arch-regs artifacts +
firmware intc cause headers (DMCA 1201(f) interoperability).**
