# The SoC-Fabric Perimeter (Layer-A)

This page is the **security-lens consolidation** of the `sprot` ("**s**lave
**prot**ection") hardware that forms **Layer-A** of the Cayman / Vision-Q7 GPSIMD
trust model: the *fabric-edge* perimeter that every AXI transaction must cross on
its way from a master (a PCIe endpoint, an SDMA channel, a D2D link, the Q7
itself) into the on-die IO fabric. Layer-A is the **silicon-enforced** ring; it
sits below the firmware capability checks and above the raw AXI wires. Three
register blocks build it, and they split cleanly into **decide / watch / respond**:

| block | perimeter role | what it does | CSR page |
|-------|----------------|--------------|----------|
| **`amzn_remapper` / `user_remapper`** | **DECIDE** — address/ID firewall | per-master egress CAM: match `{masked 58-bit addr + 10-bit AXI ID}`, allow/deny rd+wr independently, optionally remap, stamp `AxPROT` (privileged variant only) | [`../csr/remapper.md`](../csr/remapper.md) |
| **`nsm`** | **WATCH** — AXI transaction-integrity watchdog | inline timing/protocol-shape monitor on a PCIe master's AXI path; on 9 malformed shapes it latches, injects an error response, and can drive PCIe isolation | [`../csr/nsm.md`](../csr/nsm.md) |
| **`qos_prot` (NTS)** | **RESPOND** — no-target / deny terminator | locally terminates a transaction whose slave is absent **or** whose access was denied, with `SLVERR`/`DECERR` + `0xDEADBEEF` poison | [`../csr/qos-prot.md`](../csr/qos-prot.md) |

This page does **not** re-table every field — the three CSR pages above are the
byte-exact register references and are authoritative. Here we draw the
**perimeter as one machine**: the trust split that makes it an isolation boundary,
the three terminators and how they differ in *CSR materialization* of the same
wire bytes, and the reimplementation-grade C for the two algorithms a rebuild gets
wrong most often — the **CAM match/remap** and the **NTS response-injection**.
Forward references: the fault→isolation→IRQ flow is
[`../interrupt/nsm-flow-unified.md`](../interrupt/nsm-flow-unified.md) (#942); the
PCIe isolation triggers are
[`../interrupt/pcie-hbm-tpb-d2d-triggers.md`](../interrupt/pcie-hbm-tpb-d2d-triggers.md);
boot-time arming is [`boot-arming-fault-recovery.md`](boot-arming-fault-recovery.md)
(#948); the firmware trust chain that wraps Layer-A is
[`trust-chain-threat-model.md`](trust-chain-threat-model.md) (#951); the fleet
census of every `sprot` leaf is
[`../address/pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md).

> **PROVENANCE.** Every register/field/offset/reset on this page is read directly
> from the shipped Cayman (NC-v3) register schemas
> `csrs/sprot/{nsm,amzn_remapper,user_remapper,qos_prot}.json` — RTL-generated,
> binary-derived vendor data, `jq`-counted from those exact files (never a
> decompile grep). The page default is `[HIGH · OBSERVED]`; claims that depart from
> it carry an explicit `[conf · prov]` tag, `prov ∈ {OBSERVED,
> INFERRED, CARRIED}`. The comparator/responder *algebra* (the C pseudocode) is
> `[MED · INFERRED]` from field names + semantics — the schema describes the
> storage, not the gate RTL. v5/Maverick is **header-OBSERVED only** → any v5
> *interior* behaviour is `[* · INFERRED]`, with **one** observed exception: the
> Maverick `nsm.json` itself ships on disk byte-identical to Cayman (§7).

> **WHAT LAYER-A IS NOT.** None of the three blocks carries a **VMID**. A full-file
> scan of all four schemas for `vmid|secure|privileged|domain|nonsecure` returns
> field-name hits **only** in the AxPROT generator (`master_prot`) — never a VMID
> gate. VMID is assigned **upstream** in the DMA engine (`udma_gen_ex` VMPR/VMADDR,
> per queue) before a transaction reaches the fabric edge. Layer-A gates on
> **`{address region, 10-bit AXI master-ID}`** and **stamps** `AxPROT`; it does not
> check a guest VM identity. The two are disjoint, complementary virtualization
> layers — VMID = which guest owns the buffer; AXI ID = which hardware master
> issued the bus cycle. `[HIGH · OBSERVED by absence]`

---

## 1. The trust split — why this is an isolation boundary

The perimeter is the **same IP, asymmetrically populated** for two privilege
domains, and the asymmetry *is* the isolation primitive. One reset value, read
byte-exact from each JSON, decides whether the firmware path is safe and the guest
sandbox is contained:

| primitive | privileged (`amzn_*`) | guest (`user_*`) | source field |
|-----------|-----------------------|------------------|--------------|
| **CAM miss policy** | **fail-CLOSED** — `pass_on_miss = 0x0` (DENY) | **fail-OPEN** — `pass_on_miss = 0x1` (PASS) | `{amzn,user}_cam_pass_on_miss.{rd,wr}_pass_on_miss` |
| **empty-entry ID match** | strict — `id_cmp_dis` reset `0x0` (must match ID) | lax — `id_cmp_dis` reset `0x1` (address-only) | `*_cam.rd_buf_1.id_cmp_dis` |
| **`AxPROT` generation** | **YES** — `arprot[6:4]=0x2`, `awprot[2:0]=0x2` | **NONE** — no `master_prot` register exists | `amzn_remapper.control.master_prot` |
| **management over peer CAM** | wipe (`user_cam_ctl`) + bypass-ID (`*_byp_id`) | n/a — guest cannot reach these | `amzn_remapper.control.user_cam_*` |
| **window size** | `0x1000` (4 KiB, 87 reg-defs) | `0x800` (2 KiB, 28 reg-defs) | `RegFile.SizeInBytes` |

> **WALL — the isolation primitive in one line.** The privileged remapper boots
> **deny-by-default** so a half-configured boot cannot leak; the guest remapper
> boots **permissive** so an un-provisioned guest is not bricked — the guest is
> *contained* by the surrounding privileged firewall (which can wipe and override
> the guest CAM) and the NTS error path, **not** by its own default-deny. A
> Vision-Q7 control-plane rebuild that inverts either reset value has bricked the
> firmware path (`amzn`→fail-open removes the whitelist) **or** torn the sandbox
> open (`user`→fail-closed deadlocks every un-provisioned guest master). These
> resets are **frozen across Sunda / Cayman / Mariana** — a deliberately immutable
> invariant. Full field detail and the cross-gen freeze table are in
> [`../csr/remapper.md`](../csr/remapper.md) §4.1 / §7; this is the perimeter
> framing, not a re-derivation. `[HIGH · OBSERVED]`

> **NOTE — the AxPROT stamp is the privilege ratchet.** Reset `0x2` sets only
> AxPROT bit `[1]` → in the ARM encoding (`[0]`=privileged, `[1]`=non-secure,
> `[2]`=instruction) that is **non-secure / privileged / data**. The guest variant
> physically has **no** `master_prot` register, so a guest master can pass / deny /
> remap its own traffic but can **never set or override** the protection sideband.
> A rebuild that exposes `arprot`/`awprot` writability on the user side lets a
> guest claim privileged attributes on the fabric — a security defect. `[HIGH ·
> OBSERVED field/reset; MED · INFERRED ARM encoding]`

---

## 2. The remapper CAM — the DECIDE algorithm (reimplementation-grade) `[HIGH · OBSERVED layout; MED · INFERRED algebra]`

A CAM entry is **6 words / 192 bits**, accessed **indirectly** (write `rd_idx`,
read `rd_buf_0..5`; fill `wr_buf_0..4` then commit via `wr_buf_5.wr_index`). The
full per-word bit layout, the indirect protocol, and the read/write lane shuffle
are tabled byte-exact in [`../csr/remapper.md`](../csr/remapper.md) §3 — this page
gives the **decode + match + remap** as one C routine a rebuild can compile
against. The field bit-positions below are read from `amzn_remapper.json`:
`rd_buf_1` = `valid[31] wr_pass[30] rd_pass[29] id_cmp_dis[28]
intlv_en[27] remap_en[26] cmp_addr[25:0]`; `rd_buf_5` = `id[25:16] remap_addr[13:0]`.

```c
/* One decoded CAM entry: 6x32-bit words -> 192-bit logical record.
 * Widths are OBSERVED from amzn_remapper.json rd_buf_* bit-positions;
 * the masked-compare + region-remap algebra is INFERRED from field semantics. */
typedef struct {
    uint64_t cmp_addr;       /* [57:0]  rd_buf_0[31:0] | rd_buf_1[25:0]<<32      */
    uint64_t cmp_addr_mask;  /* [57:0]  rd_buf_2[31:0] | rd_buf_3[25:0]<<32      */
    uint64_t remap_addr;     /* [45:0]  rd_buf_4[31:0] | rd_buf_5[13:0]<<32      */
    uint16_t id;             /* [9:0]   rd_buf_5[25:16] -- 10-bit AXI master-ID  */
    bool valid;              /* rd_buf_1[31]  -- entry participates in the match */
    bool wr_pass;            /* rd_buf_1[30]  -- on hit: 1=write PASS, 0=DENY    */
    bool rd_pass;            /* rd_buf_1[29]  -- on hit: 1=read  PASS, 0=DENY    */
    bool id_cmp_dis;         /* rd_buf_1[28]  -- 1=ignore AXI ID (address-only)  */
    bool intlv_en;           /* rd_buf_1[27]  -- 1=apply the interleave bit-swap */
    bool remap_en;           /* rd_buf_1[26]  -- 1=rewrite address; 0=pass-thru  */
} cam_entry_t;

typedef enum { VERDICT_PASS, VERDICT_DENY } verdict_t;

/* Per-block policy = the pass_on_miss reset that defines the trust domain.
 * amzn: {rd,wr}_pass_on_miss = 0 (fail-CLOSED).  user: = 1 (fail-OPEN). */
typedef struct {
    cam_entry_t entries[128];   /* addressing ceiling 128; synth depth unknown  */
    unsigned    n_entries;
    bool        rd_pass_on_miss; /* amzn_cam_pass_on_miss.rd_pass_on_miss reset  */
    bool        wr_pass_on_miss; /* amzn_cam_pass_on_miss.wr_pass_on_miss reset  */
    uint8_t     swap_bit_0;      /* {amzn,user}_interlv_swap.swap_bit_0[5:0]     */
    uint8_t     swap_bit_1;      /* {amzn,user}_interlv_swap.swap_bit_1[13:8]    */
} remapper_t;

/* TCAM masked region match: mask bit 1 = don't-care (classic TCAM).
 * Optional 10-bit AXI-ID qualifier when id_cmp_dis == 0. */
static inline bool cam_hit(const cam_entry_t *e,
                           uint64_t in_addr /*[57:0]*/, uint16_t in_id /*[9:0]*/)
{
    if (!e->valid) return false;
    if (((in_addr ^ e->cmp_addr) & ~e->cmp_addr_mask) != 0) return false;     /* region */
    if (!e->id_cmp_dis && (((in_id ^ e->id) & 0x3FF) != 0)) return false;     /* AXI ID */
    return true;
}

/* DECIDE: resolve one AXI transaction at the fabric edge.
 * Returns PASS/DENY and, on PASS+remap, the rewritten 58-bit out_addr.
 * On DENY the caller steers the txn into the NTS terminator (section 3) and
 * latches addr_denied_{lo,hi}; remapper itself encodes NO response code. */
static verdict_t remapper_decide(const remapper_t *rm, bool is_write,
                                 uint64_t in_addr, uint16_t in_id,
                                 uint64_t *out_addr)
{
    *out_addr = in_addr;
    for (unsigned i = 0; i < rm->n_entries; i++) {
        const cam_entry_t *e = &rm->entries[i];
        if (!cam_hit(e, in_addr, in_id)) continue;

        /* HIT: per-direction allow/deny */
        bool allow = is_write ? e->wr_pass : e->rd_pass;
        if (!allow) return VERDICT_DENY;

        /* REMAP: replace only the low 46 bits (LOCAL base relocation).
         * The upper 12 bits [57:46] -- DIE/CAYMAN_ID/PCIe-attr/OK_TO_FAIL -- are
         * NEVER rewritten: intra-die relocation, never a cross-die reroute. */
        if (e->remap_en) {
            *out_addr = (in_addr & ~(uint64_t)0x3FFFFFFFFFFFull)   /* keep [57:46] */
                      | (e->remap_addr & 0x3FFFFFFFFFFFull);       /* take [45:0]  */
        }
        /* INTERLEAVE: exchange two named bit positions (bank/stripe swap). */
        if (e->intlv_en) {
            uint64_t b0 = (*out_addr >> rm->swap_bit_0) & 1u;
            uint64_t b1 = (*out_addr >> rm->swap_bit_1) & 1u;
            if (b0 != b1) {
                *out_addr ^= (1ull << rm->swap_bit_0);
                *out_addr ^= (1ull << rm->swap_bit_1);
            }
        }
        return VERDICT_PASS;
    }

    /* MISS: the entry verdict is NOT used -- fall through to pass_on_miss.
     * This is the fail-CLOSED (amzn) / fail-OPEN (user) trust boundary. */
    bool miss_pass = is_write ? rm->wr_pass_on_miss : rm->rd_pass_on_miss;
    return miss_pass ? VERDICT_PASS : VERDICT_DENY;
}
```

> **QUIRK — the compare is 58-bit, the remap is 46-bit (deliberate).** The
> comparator sees the full SoC physical address `cmp_addr[57:0]` =
> `LOCAL[46:0] + DIE[47] + CAYMAN_ID[53:48] + CAYMAN_ID_VALID[54] + RESERVED[55] +
> PCIE_ATTR_RELAXED_ORDERING[56] + OK_TO_FAIL[57]`, so one masked compare can
> region-match by die, mesh chip-id, PCIe attribute, ok-to-fail, or local byte
> range at once. But `remap_addr` is only **46 bits** — the rewrite can relocate a
> base *within a die / within a region* and can **never** touch `DIE[47]` or
> `CAYMAN_ID`. A rebuild that widens the remap write is a cross-die-reroute bug.
> `[HIGH · widths OBSERVED; MED · INFERRED interpretation]`

> **NOTE — the supervisor reaches into the guest, never the reverse.** The
> privileged remapper carries `user_cam_ctl.cam_clr` (wipe the entire guest CAM to
> reset) and `user_cam_{wr,rd}_byp_id` (nominate a masked AXI master-ID whose
> traffic **skips** the guest policy — e.g. a trusted DMA master firmware exempts).
> These live **only** in the `0x1000` privileged window; the guest's `0x800` window
> cannot reach them and cannot bypass *itself*. The guest CAM's own pass/deny stats
> are mirrored a *second time* inside the `amzn` file (`user_cam_stats @0x600`,
> `user_cam_errors @0x380`) so the privileged ISR accounts every guest event. The
> 3.2x file-size delta (89,250 vs 27,816 B) **is** this supervisor surface. `[HIGH ·
> OBSERVED]`

---

## 3. The NTS terminator family — the RESPOND algorithm

A denied transaction (from §2) and a transaction whose slave is **absent**
(powered off / unmapped / flushed) share one fate: the **NTS (No-Target-Slave)**
responder terminates them locally so the fabric never hangs against a missing or
forbidden target. NTS lives in the QoS half of the FIS
([`../csr/qos-prot.md`](../csr/qos-prot.md) §4a). Its reset posture is **fail-safe
by construction**, byte-exact from `qos_prot.json`:

| register (`nts_amzn`) | abs off | field | reset | meaning |
|-----------------------|---------|-------|-------|---------|
| `read_response` | `0x408` | `val[1:0]` | **`0x2`** | NTS read resp: `00`=OK, `01`=unused, `10`=**SLVERR**, `11`=DECERR |
| `read_data` | `0x40c` | `val[31:0]` | **`0xdeadbeef`** | poison RDATA, **replicated across the bus** to fill a 256-bit beat |
| `write_response` | `0x410` | `val[1:0]` | **`0x2`** | NTS write resp: same encoding, default SLVERR |

```c
/* NTS responder programming -- fail-safe defaults (qos_prot.json nts_amzn).
 * One 32-bit poison word, hardware-replicated across the beat. */
typedef struct {
    uint8_t  read_response;   /* nts_amzn.read_response.val[1:0]  reset 0x2 SLVERR */
    uint32_t read_data;       /* nts_amzn.read_data.val[31:0]     reset 0xDEADBEEF  */
    uint8_t  write_response;  /* nts_amzn.write_response.val[1:0] reset 0x2 SLVERR  */
    bool     enable;          /* nts_amzn.control.enable -- OR'd with HW no-target  */
    bool     mode;            /* nts_amzn.control.mode  0=NO-TARGET, 1=BLOCK         */
    bool     flushed;         /* nts_amzn.status.flushed                            */
} nts_t;

/* RESPOND: inject the synthetic AXI completion. Called for BOTH the
 * absent-slave path AND the remapper-DENY path -- one terminator per FIS.
 * beat_bytes = data-bus width in bytes (e.g. 32 for a 256-bit beat). */
static void nts_respond(const nts_t *nts, bool is_write,
                        uint8_t *out_resp /*[1:0]*/,
                        uint8_t *out_rdata, unsigned beat_bytes)
{
    if (is_write) {
        *out_resp = nts->write_response;        /* 0x2 SLVERR (or 0x3 DECERR)      */
        return;                                  /* write beat: no data payload     */
    }
    *out_resp = nts->read_response;             /* 0x2 SLVERR                       */
    /* Replicate the single 32-bit poison word across the whole data beat:
     * the master unblocks with data + an error RRESP, and any consumer sees an
     * obviously-poisoned 0xDEADBEEF pattern rather than stale fabric contents. */
    for (unsigned b = 0; b < beat_bytes; b++)
        out_rdata[b] = ((const uint8_t *)&nts->read_data)[b & 3];
}
```

> **CORRECTION — the `0xDEADBEEF` materialization differs per terminator; the wire
> bytes do not (the #942 deadbeef-count correction).** The same poison appears on
> the AXI data lanes regardless of which block injects it, but it is **stored**
> differently in CSRs, and a raw text grep over-counts the qos_prot copy:
>
> | block | materialized `0xDEADBEEF` registers | how a 256-bit beat is filled |
> |-------|------------------------------------|------------------------------|
> | **`nsm`** | **8** — `rd.error_data_0..7` @ abs `0x21c..0x238`, each a distinct stored `val[31:0]` RW word | the **full 256-bit beat is laid out in CSRs** (8x 32-bit) — firmware can reprogram any lane |
> | **`qos_prot` NTS** | **1** — `nts_amzn.read_data` @ abs `0x40c`, one `val[31:0]` RW word | **replicated in hardware** across the bus width (1 register → N lanes) |
> | **remapper** | **0** | holds **no** response/data register — it *decides* DENY and **delegates** the response to NTS |
>
> The `jq`-counted `0xDEADBEEF` *register* totals are **8 / 1 / 0**. A raw
> `rg -ci deadbeef qos_prot.json` returns **2**, but the second hit is the field
> **Description** text (`"… default=deadbeef"`, line 1820), not a second
> register — a count-grep inflation, not a second materialized word. The
> authoritative count is **one** materialized NTS poison register, replicated by
> the datapath. `SLVERR = 0x2` and `0xDEADBEEF` are the region-wide convention
> shared by all three terminators. `[HIGH · OBSERVED]`

> **NOTE — the remapper DECIDES, the NTS RESPONDS.** The remapper schema encodes
> **no** AXI response code for a deny (no `read_response`/`write_response` field;
> `jq`-verified absent). A CAM miss/DENY terminates the transaction, latches its
> `[57:0]` address into `addr_denied_{lo,hi}`, increments the deny counter, and
> raises `fis_sprot_intr[0]` — but the actual `SLVERR`/`DECERR` + `0xDEADBEEF`
> on the wire is emitted by **this** NTS responder. One local AXI terminator serves
> the whole FIS sprot region: it is both the *missing-slave* path and the
> *access-denied* path. `[HIGH · split OBSERVED; MED · INFERRED deny-uses-NTS-path]`

> **NOTE — NTS read timeout boots armed.** `nts_isolation.ctrl.rd_timeout_en`
> resets to **`0x1`** (`qos_prot.json`): a stuck read into a missing slave is
> bounded even before firmware touches the FIS. `wr_timeout_en` boots `0x0`. The
> isolation controls (`{rd,wr}_reset`, `{slv,mstr}_slice_reset`) let firmware drain
> the NTS pending counters and SW-reset the AXI slices on either side of the NTS so
> a stuck master can be isolated and the FIS re-initialised without a full block
> reset. `nts_isolation` is **Cayman-new** (absent on Sunda). `[HIGH · OBSERVED]`

---

## 4. The NSM watchdog — the WATCH lane and its isolation feed

`nsm` is the third leg: an **AXI transaction-integrity watchdog** inline on a
single PCIe master's AXI path, between PCIe ingress and the IO fabric. It does
**not** gate access (no AxPROT, no master-ID, no region — verified by absence over
all 74 bitfields); it watches **timing and protocol shape only** and runs the
chain *transaction → fault → isolation → IRQ*. The nine malformed shapes, the full
74-field map, the timeout model, and the recovery order are exhaustively tabled in
[`../csr/nsm.md`](../csr/nsm.md) — the perimeter-relevant slice is:

**Nine protocol-shape causes** (4 write + 5 read, `jq`-counted = 9), one per nibble
of `wr.status` / `rd.status`:

| class | write causes (`wr.status`) | read causes (`rd.status`) |
|-------|----------------------------|----------------------------|
| spurious response (no outstanding match) | `error_1_b_no_match_aw_ro` | `error_2_r_no_match_ar` |
| response-latency timeout | `error_2_b_to_aw_timeout` | `error_3_r_to_ar_timeout` |
| request handshake stall (`valid→ready`) | `error_3_awvalid…`, `error_4_wvalid…` | `error_4_arvalid…` |
| beat-count / `RLAST` integrity | — *(writes have no slave-returned length)* | `error_0_rlast_before…`, `error_1_rlast_not_set` |

**Synthetic error-response injection.** On any cause NSM (a) latches a sticky bit,
(b) injects an AXI error response — `BRESP = wr.cfg_1.axi_bresp` / `RRESP =
rd.cfg_1.axi_rresp`, both `[1:0]` reset **`0x2` = SLVERR** (`2'b11 = DECERR`),
`jq`-verified — plus a **256-bit poison payload from the 8x `rd.error_data_*`
registers** (§3 table), and (c) optionally drives PCIe isolation. So unlike NTS
(one replicated word), NSM materializes the **entire** poison beat in CSRs.

**Default arming posture** (composite `cfg_1` resets, `jq`-verified): out of reset
NSM auto-isolates on **spurious / long / short** responses but **not** on a plain
error response —

```text
wr.cfg_1 = axi_bresp 0x2 | enter_iso_on_spurious_rsp[3]=1            -> 0x0000000a
rd.cfg_1 = axi_rresp 0x2 | enter_iso_on_long[3]=1 | _on_short[4]=1
                         | _on_spurious[5]=1                          -> 0x0000003a
(enter_isolation_mode_on_rsp_error reset 0 on BOTH directions)
```

**The PCIe isolation state-machine feed.** A latched NSM violation feeds two
fabrics, both confirmed verbatim in the Cayman `intc/` YAMLs:

* **PEB apex INTC** — `intr_peb_nsm_axi_timeout` is the **112th** trigger entry =
  **apex index 111**, `edge_triggered: false` (**LEVEL**), `critical: 1`
  (**CRITICAL**), description *"PEB SPROT NSM timeout or error interrupt"*.
* **PCIe isolation SM** — `pcie_triggers.yaml` `reset_handshake_intr[8..15]` is the
  shared isolation-event vector; NSM is source **`[11]`**, the NTS sibling is
  **`[12]`**:

  | idx | name | source |
  |-----|------|--------|
  | `[8]` | `isolation_sm_linkdown_detected` | link-down |
  | `[9]` | `isolation_sm_flr_detected` | Function-Level Reset |
  | `[10]` | `isolation_sm_sbr_detected` | Secondary Bus Reset |
  | **`[11]`** | **`isolatio_sm_nsm_axi_timeout_detected`** | **NSM AXI timeout (this watchdog)** |
  | **`[12]`** | **`isolation_sm_nts_axi_timeout_detected`** | **NTS AXI timeout (the `qos_prot` sibling)** |
  | `[13]` | `isolation_sm_pir_detected` | Pacific-Initiated Reset |
  | `[14]` | `isolation_sm_isolation_mode_enter` | isolation entered |
  | `[15]` | `isolation_sm_isolation_mode_exit` | isolation exited |

> **QUIRK — verbatim typo in the shipped name.** Index `[11]` is spelled
> **`isolatio_sm_nsm_axi_timeout_detected`** in `pcie_triggers.yaml` — missing the
> trailing `n` in "isolatio". It is the **only** index in `[8..15]` using the
> misspelled `isolatio_sm` prefix; `[8..10]` and `[12..15]` all use the correct
> `isolation_sm`. A reimplementation matching the binary's symbol table must
> reproduce the typo on this one source. The NSM ↔ NTS adjacency at `[11]`/`[12]`
> is the perimeter signature: the **WATCH** lane and the **RESPOND** lane both feed
> the same isolation SM, one rank apart. `[HIGH · OBSERVED]`

The unified fault→isolation→IRQ flow (debounce, FSM state, quiesce/recovery order)
is [`../interrupt/nsm-flow-unified.md`](../interrupt/nsm-flow-unified.md) (#942);
the broader PCIe/HBM/TPB/D2D trigger map is
[`../interrupt/pcie-hbm-tpb-d2d-triggers.md`](../interrupt/pcie-hbm-tpb-d2d-triggers.md).

---

## 5. The perimeter as one machine `[HIGH · structure OBSERVED; MED · INFERRED ordering]`

How a single AXI transaction crosses Layer-A, end to end:

```text
  master AXI txn  {addr[57:0], id[9:0], rd/wr, burst}
        |
        v  -- WATCH (nsm, inline on PCIe master path) -----------------+
   protocol-shape OK?  (9 causes: stall / timeout / spurious / RLAST)  |  violation
        | yes                                                          v
        v  -- DECIDE (remapper CAM, fabric egress) --       inject SLVERR/DECERR
   cam_hit?                                                  + 8x error_data 256-bit beat
     |- HIT  -> rd_pass/wr_pass ? PASS(+remap+interleave) : DENY       |
     +- MISS -> pass_on_miss ?  amzn:DENY(0x0) / user:PASS(0x1)        v
        | PASS                          | DENY            drive PCIe isolation SM
        v                               v                 pcie_triggers[11] (edge)
   forward to slave            steer into NTS responder   + peb_intc idx111 (level,crit)
        |                      (qos_prot nts_amzn)                      |
   slave present? -- no ------> RESPOND: SLVERR + 0xDEADBEEF <----------+
        | yes                  (1 replicated word; remapper delegates here)
        v                               |
   normal completion          synthetic completion -> master unblocks, fabric stays alive
```

Three reset facts make the whole perimeter **boot-transparent but fail-safe** — a
rebuild must honour all three:

* **NSM** ships **bypassed** (`control.bypass.enable = 0x1`); secure firmware clears
  it to **arm** the watchdog.
* **QoS shaper** ships **transparent** (`csr.control.chicken = 0x1`); nothing is
  shaped until firmware clears it. But the **NTS responder** is **armed at reset**
  (`read_response`/`write_response = 0x2 SLVERR`, `read_data = 0xDEADBEEF`,
  `rd_timeout_en = 0x1`) — a missing slave on the boot path is *never* silently
  hung.
* **Remapper** ships with its **trust split frozen**: privileged fail-CLOSED
  (`0x0`), guest fail-OPEN (`0x1`).

> **INFERRED ordering.** The relative ordering of *inject-response* vs
> *enter-isolation* within NSM, whether `bypass.enable` auto-clears on
> isolation-exit, and the exact point a remapper DENY hands off to NTS are
> reconstructed from field names + recovery notes, not stated explicitly in the
> schemas. The `peb_intc → Q7/GIC` vector for `intr_peb_nsm_axi_timeout` is not in
> the shipped schema. `[MED · INFERRED]`

---

## 6. Adversarial self-verification

The five perimeter-critical claims, checked against the exact JSON
files (`jq`, single-file, never a folder grep):

| # | claim | check against | result |
|---|-------|---------------|--------|
| 1 | NSM materializes **8** `0xdeadbeef` poison words @ abs `0x21c..0x238` | `nsm.json` `rd.error_data_*` | **8**, each `val[31:0]` RW reset `0xdeadbeef` ✓ |
| 2 | SLVERR/DECERR via `axi_bresp`/`axi_rresp` `[1:0]` reset `0x2` | `nsm.json` `{wr,rd}.cfg_1` | both `[1:0]` reset `0x2` ✓ |
| 3 | qos_prot NTS materializes **1** `0xdeadbeef` (not 2) | `qos_prot.json` `nts_amzn.read_data` | **1** register @ abs `0x40c`; 2nd raw hit = Description text ✓ |
| 4 | remapper `pass_on_miss` amzn `0x0` (closed) / user `0x1` (open) | both `*_remapper.json` | amzn rd/wr `0x0`, user rd/wr `0x1` ✓ |
| 5 | `master_prot` AxPROT `0x2` amzn-only; user has none | both `*_remapper.json` | amzn `arprot[6:4]`/`awprot[2:0]` reset `0x2`; user `master_prot` count **0** ✓ |

CAM layout (`rd_buf_1`: `valid[31] wr_pass[30] rd_pass[29] id_cmp_dis[28]
intlv_en[27] remap_en[26] cmp_addr[25:0]`; `rd_buf_5`: `id[25:16] remap_addr[13:0]`),
the empty-entry `id_cmp_dis` split (amzn `0x0` / user `0x1`), the NTS response
encoding (`01`=unused), the `pcie_triggers[11]/[12]` NSM/NTS adjacency + the
`isolatio_sm` typo, the apex idx-111 LEVEL/CRITICAL, and the v5 `nsm.json`
on-disk byte-identity were all re-confirmed against their source files. No claim
failed; the only correction surfaced was the qos_prot deadbeef *materialization*
count (§3, the #942 correction): **one** register, datapath-replicated — the "2"
is count-grep inflation.

---

## 7. Cross-generation

The perimeter core is **frozen from Sunda → Cayman → Mariana**: the remapper
`pass_on_miss` fail-closed/open split (`0x0`/`0x1`), the `master_prot` AxPROT
default (`0x2`), the CAM bundle structure (13 regs / 28 fields), the NTS
SLVERR/`0xDEADBEEF` fail-safe defaults, and the NSM 74-field map are byte-identical
across gens. Cayman is the intermediate that widened the AXI master-ID 8→10-bit,
added the remapper `txn_len_chk` and the QoS `nts_isolation` + NTS `mode`. Per-block
cross-gen tables: [`../csr/remapper.md`](../csr/remapper.md) §7,
[`../csr/qos-prot.md`](../csr/qos-prot.md) §6, [`../csr/nsm.md`](../csr/nsm.md) §8.

> **WALL — v5 / Maverick is header-OBSERVED, with one exception.** The Maverick
> (NC-v5) `amzn_remapper.json` / `user_remapper.json` / `qos_prot.json` ship on disk
> and their **structure / names / sizes / reset values are `[HIGH · OBSERVED]`**
> (RTL-derived JSON), but any statement about what the v5 silicon *does at runtime*
> behind a given leaf is **`[v5-interior · INFERRED]`**. The **one** block where the
> v5 register map itself is byte-grounded rather than inferred is **`nsm`**: the
> Maverick `csrs/sprot/nsm.json` is present and byte-identical to Cayman, so the NSM
> layout specifically is `[HIGH · OBSERVED]` on v5 (its v5-interior AXI dataflow /
> apex wiring stays INFERRED). Cayman is the authoritative anchor for this entire
> page. `[HIGH · OBSERVED]`

---

## 8. Reimplementation checklist `[summary]`

* **Three blocks, three roles:** remapper = **DECIDE** (CAM match/deny/remap +
  AxPROT), NSM = **WATCH** (protocol-shape watchdog), qos_prot NTS = **RESPOND**
  (SLVERR + `0xDEADBEEF` terminator). The remapper holds **no** response register;
  a DENY is *decided* there and *answered* by NTS.
* **Never invert the trust split:** privileged `pass_on_miss = 0x0` (fail-CLOSED),
  guest `= 0x1` (fail-OPEN); only the privileged variant emits `AxPROT` (`0x2`);
  the supervisor's wipe/bypass hooks over the guest CAM live **only** in the
  privileged window.
* **CAM entry = 6 words / 192 bits**, indirect-accessed; match = masked **58-bit**
  address (mask `1`=don't-care) AND optional **10-bit** AXI-ID; remap rewrites only
  `out_addr[45:0]` (never `DIE`/`CAYMAN_ID`).
* **NTS must fail safe and be replicated:** `read/write_response = 0x2 SLVERR`,
  `read_data = 0xDEADBEEF` (one register, datapath-replicated across the beat);
  `rd_timeout_en = 0x1` armed at reset.
* **NSM materializes the full beat:** 8x `error_data` poison registers, `cfg_1`
  injects `BRESP`/`RRESP` `0x2`, auto-isolates on spurious/long/short by reset but
  not on plain error-response; feeds `pcie_triggers[11]` (edge) + apex idx-111
  (level, critical).
* **Gate on `{region, AXI master-ID}`, not VMID** — VMID is the upstream DMA-engine
  layer; keep them distinct.
