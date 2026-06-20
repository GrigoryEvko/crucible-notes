# pkl INTC / SPROT Security Subtree

This page carves the **interrupt-controller and security-enforcement regions** out of
the Annapurna-Labs address-map source database (`al_address_map_db.pkl`, MAVERICK /
NC-v5) — three interlocking surfaces:

1. the **per-IP embedded INTC fleet** (13 distinct interrupt controllers, one schema
   per IP block, `type='INTC'` = **5,904** nodes — the Maverick decentralization);
2. the **errtrig `intc_4grp` PAIR fabric** (the symmetric `TRIG_0`/`TRIG_1` error-trigger
   primitive), the **`ap_intc` / IOFIC family** (incl. the Maverick `iofic_x8_msix`
   security IOFIC and the `int_sec_grp` SWOM fork) and the **`peb_intc` apex**;
3. the **per-FIS `sprot` stack** — `amzn_remapper`/`user_remapper` access-control CAMs,
   `qos_prot`/`qos_host_visible` shaper + NTS, the `nsm` AXI integrity watchdog and
   `qos_pmu`, all converging on the **amzn-fail-CLOSED / user-fail-OPEN trust boundary**.

It reuses the inert load primitive from [`pkl-db.md`](./pkl-db.md) — **read that first**;
this page never re-derives the safe reader, only the INTC/SPROT carve. The physical
interrupt-instance census (the Cayman flat-YAML counterpart) is in
[`../interrupt/physical-intc-instances.md`](../interrupt/physical-intc-instances.md); the
security synthesis in [`../security/security-synthesis.md`](../security/security-synthesis.md)
and [`../security/soc-fabric-perimeter.md`](../security/soc-fabric-perimeter.md); the live
enforcement-CSR surfaces in [`../csr/remapper.md`](../csr/remapper.md),
[`../csr/qos-prot.md`](../csr/qos-prot.md) and [`../csr/nsm.md`](../csr/nsm.md); the
per-block schema bindings in [`block-schema-xref.md`](./block-schema-xref.md).

> **WALL — MAVERICK (v5, this DB) vs CAYMAN (NC-v3, cross-check).** The DB lives at
> `arch-headers/`**`maverick`**`/ext/al_address_map_db.pkl`; every record's schema path is
> rooted under `/proj/maverick/…`. So **the DB structure, record names, bases, sizes,
> counts and schema bindings below are `[HIGH · OBSERVED]`** — read structurally out of the
> shipped pkl/json/schema-JSON (all *are* RTL-generated, binary-derived vendor data, and
> are citeable). **Any claim about what the v5 silicon *does* behind one of these addresses
> — the AXI dataflow direction of an enforcement leaf, the apex 128-input wiring — is
> `[* · INFERRED]`.** The byte-grounded generational anchor is the **Cayman (NC-v3)** flat
> address-map YAML, where the per-IP INTC fleet and the `int_sec_grp` SWOM fork are
> *absent on disk* (§7). The **enforcement core** (remapper/qos/nsm reset values) is
> **byte-identical** Cayman↔Maverick; what v5 adds is a security-hardened, decentralized
> INTC layer.

---

## 0. Safety — the same inert reader (do **not** `pickle.load`)

A `.pkl` is a program. This 216 MB file (and its 514 MB `.json` mirror) must **never** hit
`pickle.load()` / `pickle.loads()` (arbitrary code execution; a 514 MB graph slurp also
OOMs). Use the [`pkl-db.md`](./pkl-db.md) primitive: either `pickletools.genops()` (opcode
scan, *no* object construction, *never* calls `find_class`) or stream the `.json` mirror
line-by-line with `rg`/`jq`/a hand `for line in f` loop. **This page re-ran the gate as an
independent safety check** before any INTC/SPROT carve:

| check | value | `[conf · prov]` |
|-------|-------|-----------------|
| header bytes (no execution) | `80 04 95 08 00 01 00 00 00` = PROTO 4 · FRAME | `[HIGH · OBSERVED]` |
| file size | **216,631,794 B** (no drift) | `[HIGH · OBSERVED]` |
| total records (root list `len`) | **323,198** (`rg -c '"name":'` on `.json`) | `[HIGH · OBSERVED]` |
| `type` census | `REGFILE` 268,201 · `NODE` 38,573 · `TABLE` 9,848 · **`INTC` 5,904** · `MEM_CTRL` 336 · `INDIRECT_ACCESS` 336 | `[HIGH · OBSERVED]` |

> **NOTE — no drift.** Header bytes, file size, the 323,198 record count and the `type`
> census are byte-identical to the [`pkl-db.md`](./pkl-db.md) baseline (#903 anchors:
> 323,198 records, 23 fields, 5 access-domain views). The carve below streams the `.json`
> mirror as a pure-text cross-check (no pickle risk) and reads the on-disk schema JSONs
> directly for reset values.

Every record carries **23 fields**; the ones this page keys on are:

```
name  short_name  short_name_lc  size  type  offset  base  parent_names  count
```

The `name` field is the fully-qualified node path (`SECURE_INT_SENG_0_C_DIE_…`); its
first token (`USER_INT` / `SECURE_INT`) is the **access-domain view**. `short_name_lc` is
the leaf schema-instance token. `type` is one of the six census values above; `[type='INTC']`
specifically tags the **per-IP embedded INTC container** nodes (§2). All counts below were
machine-re-derived this session by streaming the `.json` — **never grepped from a
decompile**.

---

## 1. The three surfaces at a glance `[HIGH · OBSERVED]`

| # | surface | what it is | leaf count | view split (user / secure) |
|---|---------|-----------|-----------:|----------------------------|
| A | per-IP embedded INTC fleet (`type='INTC'`) | 13 per-IP-block interrupt controllers | **5,904** | 1,312 / 4,592 |
| B1 | errtrig `intc_4grp` PAIR | symmetric `TRIG_0`+`TRIG_1` error-trigger generator | `TRIG_0` 844 · `TRIG_1` 844 (per-vector); 1,372 anchor NODEs (§2b) | — |
| B2 | `ap_intc` / IOFIC family | `iofic_x1/x2/x4` + Maverick `iofic_x8_msix` + `int_sec_grp` SWOM | schema family (§2c) | — |
| B3 | `peb_intc` apex | top-of-tree PEB INTC (`peb_intc_amzn`) | **24** | 0 / 24 (secure only) |
| C | per-FIS `sprot` stack | remapper → qos → nsm enforcement | **3,616** (6 schemas) | see §4a |

The **keyword totals** that bracket these (streamed from the `.json`, byte-identical to the
mirror and to #903's family map):

| keyword (substring in `name`) | total | user_int | secure_int | `[conf · prov]` |
|-------------------------------|------:|---------:|-----------:|-----------------|
| `INTC` | **31,692** | 6,948 | 24,744 | `[HIGH · OBSERVED]` |
| `SPROT` | **4,896** | 1,224 | 3,672 | `[HIGH · OBSERVED]` |
| `FIS` | **34,384** | 8,568 | 25,816 | `[HIGH · OBSERVED]` |
| `ERRTRIG` | **5,488** | 592 | 4,896 | `[HIGH · OBSERVED]` |
| `REMAPPER` | **1,020** | 612 | 408 | `[HIGH · OBSERVED]` |
| `NSM` | **24** | 0 | 24 | `[HIGH · OBSERVED]` |

> **NOTE — keyword vs type.** `INTC`-keyword (31,692, by name substring) ⊋ `type='INTC'`
> (5,904, by record type). The keyword family folds in the register-leaf REGFILEs *inside*
> each INTC; the `type='INTC'` set is just the 13 container schemas. Both are reconciled in
> §8.

---

## 2. The INTC subtree — three controller surfaces

### 2a. The per-IP embedded INTC fleet (`type='INTC'` = 5,904; 13 schemas) `[HIGH · OBSERVED]`

This is the **Maverick decentralization**: interrupt control is pushed *into each IP block*
rather than aggregated at a few central IOFICs. Streaming the `.json` and grouping every
`type='INTC'` record by `short_name_lc` yields **exactly 13** distinct controllers:

| `short_name_lc` (pkl leaf) | count | IP block (parent chain) | schema basename |
|----------------------------|------:|-------------------------|-----------------|
| `trfc_gen_intc` | 1,136 | `…UDMA_APP_M2S_AXI_TRFC_GEN` | traffic-gen INTC |
| `fci_intc` | 672 | `…UDMA_APP_M2S_FCI_COMMON` | fabric-channel-iface INTC |
| `interrupt_ctl` | 648 | `…UDMA_APP_M2S_PMU` | PMU INTC |
| `dge_desc_lb_intc` | 576 | DGE desc-push adapter | dge dma-desc-push INTC |
| `tl_intc` | 440 | `…AMZN_UCIE_A_EW_0_D2D_TL_WRAPPER` | UCIE TL INTC |
| `ll_phy_intc` | 440 | `…D2D_LL_PHY_WRAPPER` | UCIE LL/PHY INTC |
| `dge_worker_top_intc` | 408 | DGE worker top | dge worker INTC |
| `int_ctrl_base_addr` | 336 | `…UDMA_APP_GEN` | uDMA primary int-ctrl |
| `int_ctrl_sec_addr` | 336 | `…UDMA_APP_GEN` | uDMA secondary int-ctrl |
| `fcm_intc` | 312 | FCM | fabric-channel-mux INTC |
| `dma_lb_intc` | 312 | `…DMA_LANDING_BUFFER` | dma landing-buffer INTC |
| `axi_wr_term_intc` | 144 | AXI write-terminator | axi-wr-term INTC |
| `axi_rd_term_intc` | 144 | AXI read-terminator | axi-rd-term INTC |
| **sum** | **5,904** | | |

> **CORRECTION (vs SX-ADDR-15 §2a).** The backing report named these 13 by **schema-file
> basename** (`pmu_intc.json`, `udma_primary_int_ctrl.json`, `d2d_tl_intc.json`, …). The
> tokens actually carried in the pkl/json `short_name_lc` field are the **container
> short-names** above — verified this session by streaming `type='INTC'` records and reading
> one full `name` per schema:
> `interrupt_ctl` ↔ `…_PMU_INTERRUPT_CTL` (pmu_intc),
> `int_ctrl_base_addr`/`int_ctrl_sec_addr` ↔ `…UDMA_APP_GEN_INT_CTRL_{BASE,SEC}_ADDR`
> (udma primary/secondary), `tl_intc`/`ll_phy_intc` ↔ the D2D `…WRAPPER` parents,
> `dge_desc_lb_intc` ↔ the dge dma-desc adapter, `dma_lb_intc` ↔ `…DMA_LANDING_BUFFER`.
> **The 13-count partition and the per-schema counts are byte-identical** to the report —
> only the naming granularity (schema-file vs leaf short-name) differs. Cite by
> `short_name_lc` when streaming.

`type='INTC'` view split: **user_int 1,312 / secure_int 4,592 = 5,904** (streamed). Cayman
has **zero** of these schemas on disk (§7) — this whole fleet is the v5 add.

### 2b. The errtrig `intc_4grp` PAIR primitive `[HIGH · OBSERVED]`

The error-trigger fabric is built from a **symmetric pair** of `intc_4grp` units. Streaming
the Maverick (v5) `.json` for the strict per-vector `ERRTRIG_TRIG_*` short_name:

```
ERRTRIG_TRIG_0  =  844
ERRTRIG_TRIG_1  =  844     →  TRIG_0 == TRIG_1  →  844 generator PAIRS (Maverick/v5)
```

> **CORRECTION — Maverick per-vector `TRIG_0`/`TRIG_1` is 844 each, not 1,372;
> "1,372" is the errtrig anchor-NODE total.** An earlier pass of this page cited
> `TRIG_0 == TRIG_1 == 1,372`. Re-grounding the streamed Maverick `al_address_map_db`
> shows the strict `ERRTRIG_TRIG_0` / `ERRTRIG_TRIG_1` short_names count **844 each**
> (`rg -c ERRTRIG_TRIG_0` = 844). The **1,372** figure is the errtrig *generator-anchor
> NODE* total = `errtrig_amzn.json` (**928**) + `errtrig_user.json` (**444**) = 1,372
> — the parent NODEs that *own* the `TRIG_0`/`TRIG_1`/`NOTIFIC` triplet, not a
> per-vector `TRIG` count. This matches
> [`physical-intc-instances.md`](../interrupt/physical-intc-instances.md) §7, which is
> authoritative on the v5 INTC census. The symmetric-pair *structure* is unchanged;
> only the number attached to "TRIG_0 == TRIG_1" is corrected. `[HIGH · OBSERVED]`

The equality is structural, not coincidental: every errtrig generator **always emits two
`intc_4grp` units** (an on-die-summary half and its twin), so the count of `TRIG_0` and
`TRIG_1` is identical by construction. The pair lives inside a **FIS-`0` `0x3000` errtrig
container** (`errtrig_amzn` / `errtrig_user`), holding `{TRIG_0 @+0x2000, TRIG_1 @+0x3000,
NOTIFIC @+0x4000}`. The two `intc_4grp` schemas on disk are:

| schema (`csrs/intc/`) | `Type` | `Size` | role |
|-----------------------|--------|--------|------|
| `intc_4grp_no_msix_unit.json` | `REGFILE` | `0x1000` | on-die summary (cause/mask, no MSI-X) |
| `intc_4grp_msix_unit.json` | `REGFILE` | `0x1000` | host-delivered (MSI-X-capable) |

> **NOTE — no `intc_1grp` on Maverick.** The `intc/` schema dir holds **only** the two
> `4grp` units (`ls` verified). Cayman's RDM-root `intc_1grp_msix_unit` schema is **absent**
> from the Maverick `intc/` dir — a real generational drop (§7).

ERRTRIG keyword decomposition (5,488 = user 592 / secure 4,896), by container/half:

| short_name | count | role |
|------------|------:|------|
| `ERRTRIG_TRIG_0` / `ERRTRIG_TRIG_1` | 844 each | the symmetric `intc_4grp` PAIR halves (per-vector, Maverick/v5) |
| `NOTIFIC` | 844 | per-pair notific queue |
| `errtrig_amzn` + `errtrig_user` anchor NODEs | 928 + 444 = **1,372** | the generator-anchor NODE total (owns each TRIG triplet) — *this* is the "1,372" |
| `ERRTRIG_INTC` | 528 | per-block errtrig INTC |
| `USER_ERRTRIG` | 444 | host-visible errtrig container (user 148 / sec 296) |
| `AMZN_ERRTRIG` | 400 | privileged errtrig container (secure-only) |

### 2c. The `ap_intc` / IOFIC family `[HIGH · OBSERVED]`

The classic Annapurna IOFIC line, promoted to **first-class addressable units** on
Maverick. The `ap_intc/` schema dir (verified on disk) binds:

| schema (`csrs/ap_intc/`) | `HalName` / role | `Type` · `Size` |
|--------------------------|------------------|-----------------|
| `ap_intc_grp_ctrl.json` | per-group control/cause/mask REGFILE | `REGFILE` · `0x40` |
| `ap_intc_1grp_unit.json` | `iofic_x1` | unit |
| `ap_intc_2grp_unit.json` | `iofic_x2` | unit |
| `ap_intc_4grp_unit.json` | `iofic_x4` | unit |
| `ap_intc_8grp_msix_unit.json` | **`iofic_x8_msix`** — the Maverick 256-input security IOFIC | `NODE` · `0x2000` |
| `ap_intc_grp_vec_table.json` | group vector table | — |
| `ap_intc_msix.json` / `ap_intc_pba.json` | MSI-X message / pending-bit array | — |

The `HalName` of `ap_intc_8grp_msix_unit.json` is **`iofic_x8_msix`** (verified by reading
the schema: `"HalName": "iofic_x8_msix"`, `"Type": "NODE"`) — the new MSI-X-capable
security IOFIC absent on Cayman.

**The `int_sec_grp` SWOM fork** (the security-hardening keystone of the INTC layer) lives in
`ap_intc_grp_ctrl.json`. Verified on disk:

- `int_sec_grp` register present (`AccessType: RW`), plus `int_regs_sec_grp` (write-locks);
- `InterfaceType: APB` (Maverick promoted the IOFIC group-ctrl to an APB-decoded block);
- the cause/mask sub-fields carry `"SecureReg": "SWOM"` with the verbatim description
  *"Security: SWOM with `int_sec_grp` as mask … Bits that are marked as secured will not be
  set by non-secured write, and will be read as zero by non-secured read. **Available from
  V3**."*

> **GOTCHA — IOFIC names are schema HalNames, not pkl `short_name_lc`.** Streaming the pkl
> for `short_name_lc == 'iofic_x8_msix'` (or `ap_intc*`, `peb_intc_amzn`) returns **zero**
> hits. Those tokens are the **on-disk schema basenames / HalNames** that pkl records *bind*
> (via their schema path), not the instance leaf short-names. The pkl instance leaves are
> generic (`INTERRUPT_CTL`, `PEB_INTC`, …). To census the IOFIC family you must read the
> `ap_intc/` schema dir and join on the schema binding, not grep the pkl `short_name_lc`.

### 2d. The `peb_intc` apex `[HIGH · OBSERVED]`

The top-of-tree PEB interrupt controller binds `peb_intc_amzn.json` = **24 records, all
`secure_int`**, base `0x2000008010110000…` (per-PEB/die/SENG), size `0x2000`. Streaming the
pkl `name` tokens confirms the structure:

```
PEB_INTC               (container)   ×12
PEB_INTC_TRIG          ×24            PEB_INTC_MSIX          ×24
PEB_INTC_TRIGGER_MB    ×12            PEB_INTC_MSIX_MB       ×12
PEB_INTC_NOTIFIC_MB_0..7             ×12 each
```

So **24 apex = 12 `PEB_INTC` × 2** (the `no_msix` `TRIG` half + the `MSIX` twin — the same
FLAVOR pair as the errtrig primitive, per-PEB). Each `PEB_INTC` container holds the
`TRIGGER_MB` / `MSIX_MB` / `NOTIFIC_MB_0..7` mailbox stubs plus the apex INTC.

> **OPEN (LOW).** The apex 128-input wiring (which leaf `nmi_out` → which apex bit), the
> apex-pending-bit → Q7/GIC vector hop and the firmware ISR bodies are **not** address-encoded
> in this DB — they are the firmware/interrupt-synthesis domain
> ([`../security/security-synthesis.md`](../security/security-synthesis.md)). This page
> enumerates the INTC *hardware* the apex aggregates, not the routing.

### 2e. D2D INTC placement (cross-check) `[HIGH · OBSERVED]`

The `tl_intc` (440) and `ll_phy_intc` (440) leaves of §2a are **inside the UCIE links**:
parent chains `SECURE_INT_SENG_0_C_DIE_PEB_APB_IO_AMZN_UCIE_A_EW_0_D2D_TL_WRAPPER_TL_INTC`
and `…_D2D_LL_PHY_WRAPPER_LL_PHY_INTC`. The native-UCIE generation brought its own embedded
interrupt control (`TL_INTC @+0x40000`, `LL_PHY_INTC @+0x16000` in the link); Cayman's
DWC-PCIe-as-d2d had none. This is the same silicon the PCIe/D2D fabric carve documents
([`pkl-pcie-d2d-fabric.md`](./pkl-pcie-d2d-fabric.md)).

---

## 3. Reconciliation vs the physical INTC census `[HIGH · structure / MED · numeric]`

The byte-grounded cross-check is the **Cayman flat-YAML** physical-instance count of
**1,932** ([`../interrupt/physical-intc-instances.md`](../interrupt/physical-intc-instances.md):
1,070 `no_msix` + 858 `msix` + 4 `1grp_msix` RDM root). Maverick is a *different SoC* with a
*decentralized* model, so a 1:1 numeric match is **neither expected nor claimed** — only the
**structural primitive** is invariant.

| surface | Maverick pkl | Cayman flat-YAML |
|---------|-------------:|------------------|
| `type='INTC'` per-IP embedded INTCs (13 schemas) | **5,904** | 0 (Cayman had none) |
| `intc_4grp` errtrig units | `no_msix` + `msix` fabric | 1,928 errtrig (4grp) |
| errtrig generator PAIRS (`TRIG_0`==`TRIG_1`) | **844** per-vector (1,372 anchor NODEs) | **962** pairs |
| `ap_intc`/IOFIC units (x1/x2/x4/x8_msix) | first-class addressable | "includes-only" in PMDT |
| `peb_intc` apex | **24** | 4 |
| Cayman `intc_1grp_msix_unit` (RDM root) | 0 | 4 |

The **frozen primitive** that survives across both gens is the symmetric `intc_4grp` PAIR
(`TRIG_0`==`TRIG_1`, `no_msix`/`msix` split, apex flavor-pair). What *changed* is the
addition of the 5,904 per-IP INTCs, the promotion of the IOFIC family to addressable units
(incl. `iofic_x8_msix`), and the drop of the Cayman RDM `intc_1grp`. The structural identity
is `[HIGH]`; the absolute unit counts are Maverick-specific and the cross-gen numeric delta
is `[MED]`.

> **CORRECTION — the Cayman errtrig PAIR is 962, not 642.** An earlier pass of this
> cross-gen table cited "642 pairs" for the Cayman flat-YAML side. That is wrong on
> the very file it claims to read: `rg -c ERRTRIG_TRIG_0 address_map_flat.yaml` =
> **962**, `ERRTRIG_TRIG_1` = **962**, `ERRTRIG_NOTIFIC` = **962** → **962 generator
> PAIRS** (one `notific_1_queue` each), splitting **428 USER + 534 AMZN**. This
> matches [`fis-errtrig-spad.md`](../csr/fis-errtrig-spad.md) (#930, the re-grounded
> source) and [`errtrig-fis-routing.md`](../interrupt/errtrig-fis-routing.md) (#939).
> The **962** is the raw `TRIG` pair count (BCAST mirror aliases *included*); the
> de-aliased **direct** generator count — BCAST excluded, the figure
> [`physical-intc-instances.md`](../interrupt/physical-intc-instances.md) §3 reports
> as **642** (428 USER + 214 AMZN direct generators) — is a *different axis*, not a
> contradiction. The PAIR-count column here uses the byte-grounded **962**. The
> Maverick side is **844** per-vector (1,372 errtrig anchor NODEs; §2b). The two SoCs
> differ — keep the per-gen provenance explicit. `[HIGH · OBSERVED]`

---

## 4. The SPROT / security subtree — remapper → qos → nsm `[HIGH · OBSERVED]`

### 4a. The central enforcement schema home (`csrs/sprot/*` = 3,616) `[HIGH · OBSERVED]`

Six enforcement schemas, all on disk in `csrs/sprot/`, all `REGFILE`, sizes byte-matching
the pkl node sizes. The leaf counts and view splits were streamed this session (joining
`name`-prefix to `short_name_lc`):

| schema (`csrs/sprot/`) | pkl `short_name_lc` | count | user / secure | `Size` | role |
|------------------------|---------------------|------:|---------------|--------|------|
| `qos_host_visible.json` | `qos`\* | 1,176 | 392 / 784 | `0x800` | host-visible QoS shaper |
| `qos_prot.json` | `qos`\* | 784 | 0 / 784 | `0x1000` | privileged QoS shaper + NTS |
| `user_remapper.json` | `user_remapper` | 612 | 204 / 408 | `0x800` | **guest** access-control CAM |
| `qos_pmu.json` | `qos_pmu` | 612 | 204 / 408 | `0x800` | QoS PMU (16-ctr OR) |
| `amzn_remapper.json` | `amzn_remapper` | 408 | 0 / 408 | `0x1000` | **privileged** access-control CAM |
| `nsm.json` | `nsm` | 24 | 0 / 24 | `0x1000` | AXI integrity watchdog |
| **sum** | | **3,616** | | | |

> **NOTE — `qos` leaf is one short-name, two schemas.** Both `qos_prot` and
> `qos_host_visible` instantiate as pkl leaves with `short_name_lc == 'qos'` (streamed:
> **1,960** total `qos` leaves = `qos_host_visible` 1,176 + `qos_prot` 784). The two schemas
> are distinguished by **view** and container size: the secure-only **784** slice is
> `qos_prot` (`0x1000`, privileged), the both-view **1,176** slice (user 392 / sec 784) is
> `qos_host_visible` (`0x800`). The remapper and `qos_pmu` leaves carry their full
> `short_name_lc`.

**The privilege asymmetry** (the user/secure access-domain split, streamed directly):

- **SECURE-only (privileged plane):** `amzn_remapper` (0/408), `qos_prot` (0/784),
  `nsm` (0/24);
- **BOTH views (guest twin host-visible):** `user_remapper` (204/408),
  `qos_host_visible` (392/784), `qos_pmu` (204/408).

### 4b. The `FIS_0_SPROT` in-series stack `[HIGH · OBSERVED]`

Each FIS slice's `sprot` container places **the remapper FIRST, the QoS shaper SECOND**, in
AXI series:

```
PRIVILEGED  (secure, amzn_peb)   FIS_0_SPROT (0x2000):
  +0x0000  AMZN_REMAPPER  REGFILE 0x1000  amzn_remapper.json   (FIRST  - access control)
  +0x1000  QOS            REGFILE 0x1000  qos_prot.json        (SECOND - shaper + NTS)
  sample base 0x2000008010105000
  parent: secure_int.seng_0.c_die.PEB_APB_IO.amzn_peb.fis_0.sprot

HOST-VISIBLE (user, USER_FIS)    FIS_0_SPROT (0x1000):
  +0x000   USER_REMAPPER  REGFILE 0x800   user_remapper.json
  +0x800   QOS            REGFILE 0x800   qos_host_visible.json
  sample base 0xc00c045000
  parent: user_int.seng_0.APB_IO.c_die.APB_IO.USER_FIS.PEB_FIS.fis_0.sprot
```

A representative record (`amzn_remapper`), byte-exact pkl ↔ json:

```
name = SECURE_INT_SENG_0_C_DIE_PEB_APB_IO_AMZN_PEB_FIS_0_SPROT_AMZN_REMAPPER
base = 2305843559239012352  (0x2000008010105000)   size = 4096 (0x1000)   offset = 0
type = REGFILE   parent_names = [ADDRESS_MAP, secure_int, seng_0, c_die, PEB_APB_IO,
                                 amzn_peb, fis_0, sprot]
schema = .../csrs/sprot/amzn_remapper.json
```

### 4c. The `nsm` AXI integrity watchdog (24; the separate PEB leaf) `[HIGH · OBSERVED]`

`nsm` is **not** inside `FIS_0_SPROT` — it is a separate `AMZN_PEB_NSM` leaf at offset
`0x11c000` inside `amzn_peb`, size `0x1000`, secure-only. **24 = 4 SENG × 3 dies (C/H/IO) ×
2 apertures (`PEB_APB_IO` + `PEB_APB_IO_BCAST`)** — 12 unique + 12 bcast aliases. The
`nsm.json` schema (read on disk) carries the **9 watchdog causes**:

```
error_0_rlast_before_last_rdata     error_1_rlast_not_set
error_1_b_no_match_aw_ro            error_2_r_no_match_ar
error_2_b_to_aw_timeout            error_3_r_to_ar_timeout
error_3_awvalid_to_awready_timeout error_4_arvalid_to_arready_timeout
error_4_wvalid_to_wready_timeout
```

plus `axi_bresp`/`axi_rresp` `ResetValue = 0x2` (**SLVERR**), **8× `0xdeadbeef`** (the
256-bit RDATA poison pattern), and **6× `enter_isolation_mode_on`** causes — i.e. on any of
the 9 protocol violations the watchdog drops the master into isolation, terminates with
SLVERR and poisons the read data. Live register detail in
[`../csr/nsm.md`](../csr/nsm.md).

### 4d. qos_prot NTS `[HIGH · OBSERVED]`

`qos_prot.json` carries the **no-target-slave responder** (`no_target_mode`,
`read_response`, `write_response`, `nts_isolation`, SLVERR): when a slave is absent or
flushed, the NTS terminates the AXI transaction with SLVERR/DECERR + `0xdeadbeef`. The
remapper **DENY** path reuses this same NTS termination. Live detail in
[`../csr/qos-prot.md`](../csr/qos-prot.md).

---

## 5. The amzn-fail-CLOSED / user-fail-OPEN trust boundary `[HIGH · OBSERVED]`

This is the **keystone security semantic** of the whole SoC fabric. Reset values read
**directly from the Maverick schemas** this session (field key `ResetValue`):

| schema / field | `ResetValue` | semantic |
|----------------|--------------|----------|
| `amzn_remapper.rd_pass_on_miss` | **`0x0`** | a CAM **miss → DENY** (fail-CLOSED) |
| `amzn_remapper.wr_pass_on_miss` | **`0x0`** | a CAM **miss → DENY** (fail-CLOSED) |
| `amzn_remapper.arprot` | **`0x2`** | AxPROT non-secure-privileged (**AMZN ONLY**) |
| `amzn_remapper.awprot` | **`0x2`** | AxPROT non-secure-privileged (**AMZN ONLY**) |
| `user_remapper.rd_pass_on_miss` | **`0x1`** | a CAM **miss → PASS** (fail-OPEN) |
| `user_remapper.wr_pass_on_miss` | **`0x1`** | a CAM **miss → PASS** (fail-OPEN) |
| `user_remapper.{arprot,awprot}` | *absent* | the guest CAM has **no `master_prot`** — it cannot emit AxPROT |

The semantic is spelled out verbatim in the schema's `Description` for `rd_pass_on_miss`:
*"0 - Reads that miss in the AMZN CAM are marked Deny / 1 - … marked Pass."*

> **The invariant, stated for a reimplementer.** The **amzn (privileged/supervisor) CAM is a
> whitelist-by-default firewall** — anything not explicitly entered is denied, and it stamps
> every egress transaction with AxPROT `0x2`. The **user (guest) CAM is allow-by-default** —
> a miss passes through, and it has no privilege bits to forge. The guest plane is therefore
> *fully observable from the amzn side* (every user txn ultimately crosses an amzn-domain
> enforcement leaf), while the amzn plane is *closed against the guest*. A faithful
> Vision-Q7 GPSIMD rebuild **must reset the privileged remapper `pass_on_miss` to 0 and the
> guest remapper to 1**, and must never expose `arprot`/`awprot` writability on the user
> CAM. Live CAM detail in [`../csr/remapper.md`](../csr/remapper.md).

> **WALL — this is the FROZEN core.** These reset values (`0x0`/`0x1` pass-on-miss, AxPROT
> `0x2`, the NTS, the nsm 9-causes/8×deadbeef/SLVERR) are **byte-identical Cayman↔Maverick**
> (§7). The trust boundary did **not** change generation-to-generation; only the INTC layer
> hardened. `[HIGH · OBSERVED]` (read from both gens' shipped schemas).

---

## 6. The central HOME of the cross-subtree FIS residuals `[HIGH · OBSERVED census · MED · mapping]`

The DMA, HBM and PCIe/D2D carves each found a "FIS/SPROT protection node" *residual* in
their own subtree arithmetic. **This subtree owns their central home.** The global `FIS`
keyword family = **34,384** records (user_int 8,568 + secure_int 25,816), decomposed by the
**protected-IP token** in each node's `name` (the central accounting table):

| protected-IP family | user_int | secure_int | total | sibling residual it homes |
|---------------------|---------:|-----------:|------:|---------------------------|
| UCIE / D2D | 2,960 | 8,160 | **11,120** | the UCIE-FIS carve (D2D fabric) |
| HBM | 1,864 | 5,328 | **7,192** | the HBM-FIS carve ([`pkl-hbm-subtree.md`](./pkl-hbm-subtree.md)) |
| DMA (DDMA/CDMA/UDMA) | 1,456 | 4,784 | **6,240** | the DMA-FIS carve ([`pkl-dma-subtree.md`](./pkl-dma-subtree.md)) |
| TPB | 1,088 | 3,296 | **4,384** | the TPB FIS ([`pkl-tpb-subtree.md`](./pkl-tpb-subtree.md)) |
| PEB-core (sprot/nsm/peb_intc/io_fabric) | 840 | 3,240 | **4,080** | **the central sprot/peb_intc home (this page)** |
| FABRIC | 360 | 1,008 | **1,368** | fabric FIS mirrors |
| **TOTAL** | **8,568** | **25,816** | **34,384** | == `FIS`-keyword total |

The secure/user ratio is **3.013** — the dual-aperture + privilege replication. **The
enforcement is fabric-edge-distributed**: the 3,616 `sprot`-schema records (§4a) sit at each
fabric master's egress FIS, *not* in one central block — DMA 1,352 / TPB 1,152 / PEB-core
792 / HBM 320. This is *why* every per-subtree carve saw a protection residual: the
remapper/qos/nsm stack is instantiated once per protected master.

> **MED — the residual ↔ row cross-walk.** Each sibling carve defined its residual by
> *subtree-root membership* (folding in non-FIS-named protection REGFILEs like `ddma_amzn` /
> `cdma_amzn` / the user-xbar), so a sibling's residual count differs slightly from this
> table's FIS-keyword-by-family row (e.g. the DMA residual folds in `ddma_amzn`/`cdma_amzn`
> beyond the FIS&DMA-keyword 6,240). The **order-of-magnitude, the ~3:1 ratio and the per-IP
> placement all agree**; the clean *closed* accounting is the FIS-keyword central table
> (34,384). The exact per-record cross-walk is `[MED]` — not byte-reconstructed (different
> membership predicates).

The FIS **container** schemas the enforcement sits inside (the wrappers, `[HIGH · OBSERVED]`):
`fis_control.json` (2,384, secure-only, `0x2000` — APB decode/timeout/gating), `fis_sprot_*`
(user 612 / dbug 612 / amzn 408), `fis_type_*.json` (10,376 across 16 distinct slice
schemas). The enforcement REGFILEs of §4a live *inside* these slice containers.

---

## 7. Maverick vs Cayman — the security-decentralization delta `[HIGH · OBSERVED on disk]`

| aspect | CAYMAN (NC-v3, flat-YAML) | MAVERICK (NC-v5, this DB) | conf |
|--------|---------------------------|---------------------------|------|
| `ap_intc_grp_ctrl` | 9-register MEM block; `InterfaceType=NONE`; **no `int_sec_grp`** | `int_sec_grp` (@`0xC` SWOM) + `int_regs_sec_grp` (@`0x24` write-locks) **ADDED**; **APB**-interfaced | HIGH |
| per-IP embedded INTC | **ZERO** (all 13 schemas absent on disk) | **13 schemas, 5,904** `type='INTC'` nodes (decentralized) | HIGH |
| security IOFIC | `iofic_x1/x2/x4` (MEM, no MSI-X) | **adds `iofic_x8_msix`** (256-input, MSI-X-capable) | HIGH |
| RDM-root intc | `intc_1grp_msix_unit` (4) | **absent** (`intc/` dir = 2× `4grp` only) | HIGH |
| remapper CAM core | fail-CLOSED / fail-OPEN + AxPROT `0x2` | **BYTE-IDENTICAL** (`pass_on_miss` `0x0`/`0x1`, `ar/awprot` `0x2`) | HIGH |
| qos_prot / NTS | shaper + NTS + `nts_isolation` | **BYTE-IDENTICAL** | HIGH |
| nsm watchdog | inline per-PCIe-master AXI watchdog | **BYTE-IDENTICAL** (9 causes, 8× deadbeef, `axi_bresp` `0x2`); pkl model 4 SENG × 3 die × 2 | HIGH |
| die structure | 2-die (DIE bit[47]) | **3-die** C_DIE/H_DIE/IO_DIE | HIGH |
| errtrig PAIR | `TRIG_0`+`TRIG_1` `intc_4grp` pair | **IDENTICAL** (symmetric pair) | HIGH |

> **THE HEADLINE.** The **security-enforcement core** — remapper/qos/nsm + the
> fail-CLOSED/fail-OPEN trust boundary + AxPROT `0x2` + the errtrig PAIR — is **FROZEN**
> Cayman==Maverick (byte-identical reset values + schema structure). What Maverick **adds**
> is a **security-hardened, decentralized INTC layer**: the `int_sec_grp`/`int_regs_sec_grp`
> SWOM write-locks on the IOFIC, the new `iofic_x8_msix` 256-input security IOFIC, the 13
> per-IP embedded INTCs (Cayman had **0**), and the 3-die plane. A reimplementation tracking
> Cayman gets the enforcement semantics for free; the v5 work is the per-IP INTC fan-out and
> the SWOM-gated MSI-X IOFIC.

---

## 8. Record-arithmetic closure `[HIGH · OBSERVED]`

Every count below was streamed from the `.json` this session and closes **exactly**:

| family | closure | `[conf · prov]` |
|--------|---------|-----------------|
| `INTC` keyword | 31,692 = NODE 1,044 + INTC 4,584 + REGFILE 26,064 | `[HIGH · OBSERVED]` |
| `INTC` view split | 31,692 = user 6,948 + secure 24,744 | `[HIGH · OBSERVED]` |
| `type='INTC'` | 5,904 = the 13 per-IP schemas (§2a sum) | `[HIGH · OBSERVED]` |
| `SPROT` keyword | 4,896 = NODE 1,632 + REGFILE 3,264; = user 1,224 + secure 3,672 | `[HIGH · OBSERVED]` |
| sprot enforcement (6 schemas) | 3,616 = 408 + 612 + 784 + 1,176 + 24 + 612 | `[HIGH · OBSERVED]` |
| `NSM` | 24 = 4 SENG × 3 dies × 2 apertures | `[HIGH · OBSERVED]` |
| `FIS` keyword | 34,384 = user 8,568 + secure 25,816 (by-IP table §6) | `[HIGH · OBSERVED]` |
| errtrig PAIR symmetry | `TRIG_0` 844 == `TRIG_1` 844 (Maverick/v5 per-vector; 1,372 anchor NODEs; Cayman PAIR=962) | `[HIGH · OBSERVED]` |
| `type` census | 268,201 + 38,573 + 9,848 + 5,904 + 336 + 336 = 323,198 | `[HIGH · OBSERVED]` |

JSON-sibling equivalence: every keyword count above is byte-identical between the `.pkl`
(via `genops`/guarded stream) and the `.json` mirror (via `rg`), and the `amzn_remapper`
record's `base`/`size`/`type`/`offset` are byte-exact across both (§4b).

---

## 9. The carve, in code

### 9a. Stream the INTC/SPROT subtree (no `pickle.load`) `[reimplementation]`

```python
"""
Carve the INTC + SPROT records out of al_address_map_db without ever executing
the pickle. Streams the .json mirror line-by-line (no 514 MB slurp); reconstructs
one record's (name, short_name_lc, type) at a time. The .pkl is read ONLY via
pickletools.genops() for the inert opcode gate (see pkl-db.md).
"""
import re
from collections import Counter

# the real field keys (23-field record schema, #903 / SX-ADDR-15)
RE_NAME = re.compile(r'^        "name": "([^"]*)"')
RE_SNLC = re.compile(r'^        "short_name_lc": "([^"]*)"')
RE_TYPE = re.compile(r'^        "type": "([^"]*)"')

INTC_SCHEMAS = {                      # the 13 per-IP embedded INTC leaves (sec 2a)
    "trfc_gen_intc", "fci_intc", "interrupt_ctl", "dge_desc_lb_intc",
    "tl_intc", "ll_phy_intc", "dge_worker_top_intc",
    "int_ctrl_base_addr", "int_ctrl_sec_addr",
    "fcm_intc", "dma_lb_intc", "axi_wr_term_intc", "axi_rd_term_intc",
}
SPROT_SCHEMAS = {                     # the 6 enforcement leaves (sec 4a); 'qos'=prot|host_vis
    "amzn_remapper", "user_remapper", "qos", "qos_pmu", "nsm",
}

def carve(json_path):
    intc_fleet, sprot_stack = Counter(), Counter()
    name = snlc = view = None
    with open(json_path) as f:              # streaming - never json.load() the 514 MB file
        for line in f:
            m = RE_NAME.match(line)
            if m:
                name = m.group(1)
                view = ("user" if name.startswith("USER_INT")
                        else "secure" if name.startswith("SECURE_INT") else "other")
                continue
            m = RE_SNLC.match(line)
            if m:
                snlc = m.group(1); continue
            m = RE_TYPE.match(line)
            if m:
                t = m.group(1)
                if t == "INTC" and snlc in INTC_SCHEMAS:        # type-gated INTC census
                    intc_fleet[(snlc, view)] += 1
                if snlc in SPROT_SCHEMAS:                        # sprot enforcement census
                    sprot_stack[(snlc, view)] += 1
                name = snlc = view = None
    return intc_fleet, sprot_stack

# expected: sum(intc_fleet.values()) == 5904 ; 13 distinct snlc
#           sprot 'qos' == 1960 (qos_prot 784 secure-only + qos_host_visible 1176)
```

### 9b. The amzn/user fail-mode decision (the trust boundary) `[reimplementation]`

```python
"""
Decide an AXI transaction's fate at a FIS_0_SPROT remapper leaf, mirroring the
byte-verified reset semantics (sec 5). Reads ResetValue straight from the schema
JSON; the decision is the silicon's: amzn fail-CLOSED, user fail-OPEN.
"""
import json

def remapper_reset(schema_path, field):
    """Pull a field's ResetValue from amzn_remapper.json / user_remapper.json."""
    doc = json.load(open(schema_path))     # schema JSON is small + pure data (safe)
    stack = [doc]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            if o.get("Name") == field:
                return o.get("ResetValue")
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return None

def fis_decision(domain, cam_hit, is_write, amzn_schema, user_schema):
    """
    domain : 'amzn' (privileged) | 'user' (guest)
    cam_hit: True if the address matched a CAM entry
    returns: ('ALLOW' | 'DENY', axprot)   # axprot is None for the guest CAM
    """
    field = "wr_pass_on_miss" if is_write else "rd_pass_on_miss"
    if domain == "amzn":
        pass_on_miss = int(remapper_reset(amzn_schema, field), 16)   # == 0x0 (fail-CLOSED)
        axprot = int(remapper_reset(amzn_schema,
                     "awprot" if is_write else "arprot"), 16)        # == 0x2 (AMZN ONLY)
    else:
        pass_on_miss = int(remapper_reset(user_schema, field), 16)   # == 0x1 (fail-OPEN)
        axprot = None                                                # guest cannot emit AxPROT
    allow = cam_hit or bool(pass_on_miss)
    # a DENY reuses the qos_prot NTS path: terminate AXI with SLVERR + 0xDEADBEEF (sec 4d)
    return ("ALLOW" if allow else "DENY"), axprot
```

---

## 10. Confidence ledger

**`[HIGH · OBSERVED]`** (byte-read from the shipped pkl/json/schemas, re-run this session):
the inert load gate (header `80 04 95 …`, file 216,631,794 B, 323,198 records, `type` census,
0 dangerous opcodes carried from [`pkl-db.md`](./pkl-db.md)); the 13 per-IP INTC schemas
(`type='INTC'` = 5,904, streamed, partitioned by `short_name_lc`); the symmetric errtrig PAIR
(Maverick/v5 `TRIG_0`==`TRIG_1`==844 per-vector, 1,372 errtrig anchor NODEs; Cayman PAIR=962 §3);
the `iofic_x8_msix` HalName + the `int_sec_grp`/`int_regs_sec_grp`
SWOM fork + APB interface in `ap_intc_grp_ctrl.json`; the `peb_intc` apex (24, secure, 12×2);
the 6 `sprot` enforcement schemas (3,616, with view splits and the privilege asymmetry); the
`FIS_0_SPROT` remapper-FIRST/qos-SECOND stack; the `nsm` 9-cause/8×deadbeef/SLVERR watchdog
(4×3×2=24); the **trust boundary reset values** (`amzn` `pass_on_miss` `0x0` + AxPROT `0x2`,
`user` `pass_on_miss` `0x1`, no guest `master_prot`); the FIS central-home table (34,384); the
Maverick-vs-Cayman delta (verified against both gens' `intc/` + `ap_intc/` + `sprot/` dirs);
all §8 arithmetic.

**`[MED · INFERRED]`**: the per-record cross-walk between each sibling carve's FIS residual SET
(subtree-root membership) and this page's FIS-keyword-by-IP rows (different membership
predicates, §6); the Cayman-1,932 ↔ Maverick-pkl physical-INTC count as a *structural* (not
1:1 numeric) match (§3); the runtime AXI dataflow direction of each enforcement leaf (the
"fabric-edge firewall on every master egress" reading is HIGH for placement, MED for dynamics —
see [`../security/security-synthesis.md`](../security/security-synthesis.md)).

**`[LOW · OPEN]`** (explicit non-claims): the apex 128-input wiring (which leaf `nmi_out` → which
apex bit), the apex-pending-bit → Q7/GIC vector hop, the firmware ISR bodies, and the remapper
CAM entry depth — none are address-encoded in this DB; they are the interrupt/security-synthesis
domain.
