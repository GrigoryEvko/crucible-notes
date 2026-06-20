# Physical INTC Instance Map

This page is the **physical census** of every interrupt-controller instance in the Cayman
(NC-v3) SoC address map, each bound to its register **schema** and its **trigger domain**. Where
[`intc-4group.md`](../csr/intc-4group.md) and [`intc-1group-apintc.md`](../csr/intc-1group-apintc.md)
reconstruct the three INTC *register files*, and the trigger pages
([`sdma-triggers.md`](sdma-triggers.md), [`io-fabric-triggers.md`](io-fabric-triggers.md),
[`pcie-hbm-tpb-d2d-triggers.md`](pcie-hbm-tpb-d2d-triggers.md),
[`peb-cc-topsp-triggers.md`](peb-cc-topsp-triggers.md), [`schema-atlas.md`](schema-atlas.md))
enumerate the *source events*, this page answers the orthogonal question: **how many controllers
are actually placed in silicon, where, of which flavor, and how they cascade up to the Q7**. It
grounds the abstract routing of the schema atlas in the RTL-generated flat address map.

The whole census reduces to one universal primitive — the **errtrig PAIR** (two `intc_4grp` units +
a notific queue per per-block aggregation) — plus a single 128-input **PEB apex** and a 4-instance
**RDM root**. The flavor (`msix` vs `no_msix`) tracks the **USER/AMZN privilege axis**, not the
trigger domain. The physical leaf → io-fabric → PEB-apex → Q7 cascade and the broadcast-alias
quirk are the two facts a reimplementer must internalize.

> **PROVENANCE.** Every instance name, base, size, and schema binding below was read byte-exact
> from the shipped, RTL-generated `address_map_flat.yaml` (Cayman/NC-v3,
> `output/address_map/`, **34,858** nodes), the three bound INTC CSR schemas
> (`csrs/intc/intc_{4grp_no_msix,4grp_msix,1grp_msix}_unit.json`), the trigger-list YAMLs
> (`intc/*_triggers.yaml`), and — for the cross-generation contrast only — the shipped Maverick
> `al_address_map_db` (streamed as JSON; **never** `pickle.load`-ed) and the Maverick `csrs/`
> schema headers. All are RTL-derived descriptor artifacts (lawful-interoperability RE). Every
> claim is tagged `[conf · prov]`: `HIGH`/`MED`/`LOW` × `OBSERVED` (read from an artifact) /
> `INFERRED` (reasoned from corroboration) / `CARRIED` (consolidated from a named sibling, not
> re-derived). The byte-grounded generation is **Cayman**; **Maverick (v5) is header-OBSERVED
> only**, so all v5-interior claims are flagged `INFERRED`. The final apex → Q7/GIC vector hop
> is **`[INFERRED]`** — a GIC is confirmed to exist (the CXELA500 ELA), but the pending-bit →
> vector map is firmware/HW-owned and in no shipped artifact.

---

## 0. The census at a glance `[HIGH · OBSERVED]`

The flat map contains **exactly 1,932** physical INTC instances — every node whose `json:` binds
to an `intc` schema (`rg -c 'json: csrs/intc/' address_map_flat.yaml` = 1932). They resolve to
**three** schemas:

| schema (`json:` binding) | count | shape | role |
|---|---|---|---|
| `csrs/intc/intc_4grp_no_msix_unit.json` | **1,070** | 4 grp × 32 = 128-input; 4 severity wire-ORs + `Mask_msi_x` summary; `Sunda` abort bundle | on-die aggregator (summary path → PEB apex) |
| `csrs/intc/intc_4grp_msix_unit.json` | **858** | 4 grp × 32 = 128-input; full MSI-X table (PBA/VecTable/128-entry MSIX) | host-facing leaf (MSI-X straight to PCIe host) |
| `csrs/intc/intc_1grp_msix_unit.json` | **4** | 1 grp × 32 = 32-input control surface, 128-entry over-provisioned MSIX table | the `IO_INTC_RDM` root-domain MSIX |
| **total** | **1,932** | | |

`intc_1grp_no_msix_unit.json` and `ap_intc_*` are **shipped schemas but never placed in the
Cayman flat map** (`rg -c 'intc_1grp_no_msix' = 0`, `rg -c 'ap_intc.*unit.json' = 0`) — `ap_intc`
(IOFIC) is an `Includes`-only block inside the PMDT complex (see
[`intc-1group-apintc.md`](../csr/intc-1group-apintc.md) §5). These counts equal the
[`intc-4group.md`](../csr/intc-4group.md) §8 address-map binding (1,070 / 858 / 4) exactly.

**The three physical-instance facts** that ground the routing abstraction:

1. **The errtrig PAIR is the universal primitive.** Every per-block aggregation is an *errtrig* =
   two `intc_4grp` units (`TRIG_0 @+0x0`, `TRIG_1 @+0x1000`) + a `notific_1_queue` (`@+0x2000`)
   filling a `0x3000` region. **642 direct generators** exist; *every* FIS-attached domain —
   including the "small" CC (`PREPROC`) and `TOP_SP` — lays down a physical pair (§3).
2. **The flavor tracks USER/AMZN, not the domain.** USER errtrig → `4grp_msix` (856 TRIG
   instances, host-reachable); AMZN errtrig → `4grp_no_msix` (1,068 TRIG instances, on-die
   summary). Re-derived independently and byte-equal to the errtrig-routing split (§2).
3. **The apex is `peb_intc`, a flavor pair — not a capacity pair.** The 128-input apex is
   `{PEB_INTC_TRIG_0 (no_msix) + PEB_INTC_MSIX (msix)}` of one 4grp, replicated per PEB. There is
   **no `PEB_INTC_TRIG_1`** (§4).

---

## 1. The instance table — instance · base · size · schema · domain · msix? `[HIGH · OBSERVED]`

The structural anchors below are read verbatim from `address_map_flat.yaml`. Bases are the file's
own hex strings. *(`\|` escapes a literal pipe inside a cell.)*

| instance (verbatim) | base | size | schema | trigger domain | msix? |
|---|---|---|---|---|---|
| `…_USER_FIS_SDMA_0_FIS_0_USER_ERRTRIG` (container) | `0x00000100C000000` | `0x3000` | *(node)* | SDMA | — |
| `…_USER_FIS_SDMA_0_FIS_0_USER_ERRTRIG_TRIG_0` | `0x00000100C000000` | `0x1000` | `intc_4grp_msix` | SDMA | **msix** |
| `…_USER_FIS_SDMA_0_FIS_0_USER_ERRTRIG_TRIG_1` | `0x00000100C001000` | `0x1000` | `intc_4grp_msix` | SDMA | **msix** |
| `…_USER_FIS_SDMA_0_FIS_0_USER_ERRTRIG_NOTIFIC` | `0x00000100C002000` | `0x1000` | `notific_1_queue` | SDMA | — |
| `PEB_APB_IO_0_AMZN_SE_0_SDMA_0_FIS_0_AMZN_ERRTRIG_TRIG_0` | `0x020008010002000` | `0x1000` | `intc_4grp_no_msix` | SDMA | no_msix |
| `APB_IO_0_USER_FIS_SE_0_PCIE_S0_FIS_0_USER_ERRTRIG_TRIG_0` | *(IO band)* | `0x1000` | `intc_4grp_msix` | PCIE_S0 | **msix** |
| `PEB_APB_IO_0_AMZN_IO_PCIE_A_FIS_0_AMZN_ERRTRIG_TRIG_0` | `0x020008016002000` | `0x1000` | `intc_4grp_no_msix` | PCIE_A | no_msix |
| `PEB_APB_IO_BCAST_0_0_AMZN_IO_PCIE_A_FIS_0_AMZN_ERRTRIG_TRIG_0` | `0x020008036002000` | `0x1000` | `intc_4grp_no_msix` | PCIE_A (BCAST alias) | no_msix |
| `PEB_APB_IO_0_AMZN_PEB_INTC` (apex container) | `0x02000801E090000` | `0x2000` | *(node)* | PEB apex | — |
| `PEB_APB_IO_0_AMZN_PEB_INTC_TRIG_0` | `0x02000801E090000` | `0x1000` | `intc_4grp_no_msix` | PEB apex (summary) | no_msix |
| `PEB_APB_IO_0_AMZN_PEB_INTC_MSIX` | `0x02000801E091000` | `0x1000` | `intc_4grp_msix` | PEB apex (host) | **msix** |
| `PEB_APB_IO_1_AMZN_PEB_INTC_TRIG_0` | `0x02080801E090000` | `0x1000` | `intc_4grp_no_msix` | PEB apex (summary) | no_msix |
| `PEB_APB_IO_1_AMZN_PEB_INTC_MSIX` | `0x02080801E091000` | `0x1000` | `intc_4grp_msix` | PEB apex (host) | **msix** |
| `APB_IO_0_USER_IO_INTC_RDM_MSIX` | `0x000008006C82000` | `0x1000` | `intc_1grp_msix` | INTC_RDM root | **msix** |
| `APB_IO_1_USER_IO_INTC_RDM_MSIX` | `0x000808006C82000` | `0x1000` | `intc_1grp_msix` | INTC_RDM root | **msix** |
| `PEB_APB_IO_0_USER_IO_INTC_RDM_MSIX` | `0x020008006C82000` | `0x1000` | `intc_1grp_msix` | INTC_RDM root | **msix** |
| `PEB_APB_IO_1_USER_IO_INTC_RDM_MSIX` | `0x020808006C82000` | `0x1000` | `intc_1grp_msix` | INTC_RDM root | **msix** |

> **NOTE — the `0x3000` errtrig container.** Each direct errtrig is a 3-region block:
> `…_ERRTRIG` (the `0x3000` parent NODE, no schema) → `TRIG_0 @+0x0` + `TRIG_1 @+0x1000`
> (both `intc_4grp`, the latch pair) + `NOTIFIC @+0x2000` (`notific_1_queue`, the doorbell/drop
> path). The container, `TRIG_0/1`, and `NOTIFIC` offsets are byte-verbatim from SDMA_0 and are
> the layout behind the errtrig generator of [`errtrig-fis-routing.md`](errtrig-fis-routing.md).
> `[HIGH · OBSERVED]`

> **NOTE — apex doorbell mailboxes.** Distinct from the per-errtrig `NOTIFIC` queue, the apex
> carries its own notification surface: `PEB_INTC_{0,1}_NOTIFIC_MB_0..7` (8 mailbox regions of
> `0x200` each per PEB, base band `0x…8580000400`) plus `PEB_INTC_{0,1}_MSIX_MB` (`0x200`
> @ `0x…8580000200`). These are mailbox MEM regions, **not** `intc`-schema nodes, and are not
> part of the 1,932. `[HIGH · OBSERVED]`

---

## 2. The `msix` / `no_msix` instance split `[HIGH · OBSERVED]`

The flavor partition is the host-path / on-die-path axis. Cross-tabbed by side and structural role
(all counts from `rg` over `address_map_flat.yaml`):

| flavor | side | count | composition |
|---|---|---|---|
| `4grp_msix` | USER | **856** | host-reachable errtrig leaves (all direct, never BCAST) |
| `4grp_msix` | AMZN | **2** | the two `PEB_INTC_MSIX` apexes (the **only** AMZN-side msix in the map) |
| `4grp_no_msix` | AMZN | **1,070** | 428 direct errtrig `TRIG` + **640 BCAST aliases** + 2 `PEB_INTC_TRIG_0` apexes |
| `1grp_msix` | USER | **4** | the `IO_INTC_RDM_MSIX` root-domain |
| | | **1,932** | |

**The rule** (errtrig instances only; re-derived independently of the routing page):

```
USER side  →  4grp_MSIX     = 856   (host-reachable; MSI-X straight to PCIe host)
AMZN side  →  4grp_NO_MSIX  = 1068  (= 428 direct TRIG + 640 BCAST aliases)
```

Verified byte-exact:
`rg 'intc_4grp_msix_unit' | rg 'ERRTRIG_TRIG' | rg USER` = **856**, `… rg AMZN` = **0**;
`rg 'intc_4grp_no_msix_unit' | rg 'ERRTRIG_TRIG' | rg AMZN` = **1068**, `… rg USER` = **0**.
The flavor is chosen by the instance's **privilege/role** (USER = host-reachable, AMZN =
on-die-privileged), **not** by the trigger domain — every domain appears in *both* flavors (§3).

> **QUIRK — the apex MSIX is the lone AMZN-side host-delivered MSI-X.** The 858 `msix` instances
> are 856 USER errtrig leaves + **exactly 2** AMZN `PEB_INTC_MSIX`. The apex is
> privileged-but-host-delivered: it rolls the on-die summary tree up and emits an MSI-X to the
> management core. `rg 'intc_4grp_msix_unit' | rg -c 'PEB_INTC_MSIX'` = **2**, the only
> non-USER msix. `[HIGH · OBSERVED]`

> **NOTE — MSI-X is never broadcast.** All 640 BCAST aliases are `no_msix`
> (`rg 'BCAST' | rg -c 'intc_4grp_msix_unit'` = **0**). Broadcast is an on-die aggregation
> concern (one privileged-APB CSR write fans to many FIS instances); MSI-X targets a specific
> host vector and is never fanned out. `[HIGH · OBSERVED]`

---

## 3. The per-domain instance table (errtrig pairs, by domain) `[HIGH · OBSERVED]`

Direct errtrig `TRIG` instances (BCAST excluded), tabulated by the DOMAIN token in the name
(`rg -c "_<DOMAIN>_"` over the USER-msix and AMZN-no_msix errtrig sets). The DOMAIN token sits
between the FIS-attach markers; `PREPROC == CC` (the compute cluster). A *generator* is one
`TRIG_0+TRIG_1` pair, so **generators = TRIG ÷ 2**. The trigger-leaf column is the active-trigger
count of the matching `*_triggers.yaml`.

| DOMAIN | trigger leaf (active) | USER-msix TRIG | AMZN-no_msix TRIG | generators (USER/AMZN) | schema |
|---|---|---|---|---|---|
| SDMA | `sdma_triggers` (254) | **528** | **264** | 264 / 132 | `intc_4grp_*` PAIR |
| PCIE_S0..S4 | `pcie_triggers` (228) | **80** (5 × 16) | **40** | 40 / 20 | `intc_4grp_*` PAIR |
| PCIE_A | (PCIe admin, IO) | **8** | **4** | 4 / 2 | `intc_4grp_*` PAIR |
| PCIE_U | (PCIe user, IO) | **8** | **4** | 4 / 2 | `intc_4grp_*` PAIR |
| PCIE_M | (PCIe master, PEB) | **8** | **4** | 4 / 2 | `intc_4grp_*` PAIR |
| D2D | `d2d_triggers` (216) | **64** | **32** | 32 / 16 | `intc_4grp_*` PAIR |
| TOP_SP | `top_sp_triggers` (82) | **80** | **40** | 40 / 20 | `intc_4grp_*` PAIR |
| TPB | `tpb_triggers` (216) | **32** | **16** | 16 / 8 | `intc_4grp_*` PAIR |
| PREPROC (CC) | `cc_triggers` (98) | **16** | **8** | 8 / 4 | `intc_4grp_*` PAIR |
| HBM | `hbm_triggers` (223) | **16** | **8** | 8 / 4 | `intc_4grp_*` PAIR |
| INTC_RDM | (RDM fabric-master) | **8** | **4** | 4 / 2 | `intc_4grp_*` PAIR |
| **direct subtotal** | | **856** | **428** | **428 / 214 = 642 generators** | |
| PLUS PEB apex (§4) | `peb_intc_triggers` (128) | 2 (MSIX) | 2 (`TRIG_0`, no_msix) | — | flavor pair |
| PLUS `IO_INTC_RDM` root | — | 4 (1grp) | — | — | `1grp_msix` |

`rg`-verified totals: USER-msix direct = **856**, AMZN-no_msix direct = **428**
(`wc -l` over the filtered sets). PCIe aggregate USER = 80(S) + 8(A) + 8(U) + 8(M) = **104**
(matches the [`intc-4group.md`](../csr/intc-4group.md) §8 cross-check `pcie 104`).

> **KEY FINDING — every domain gets a physical pair.** The capacity-fit observation that CC(98)
> and TOP_SP(82) "fit one 4grp" (≤ 128) is a *logical* statement; **physically** every
> FIS-attached domain — including `PREPROC` and `TOP_SP` — instantiates a `TRIG_0+TRIG_1` pair,
> because the errtrig generator always emits **two** `intc_4grp` units regardless of how many
> trigger bits the domain actually fills. The only single-`4grp` instances in the whole map are
> the PEB apex `TRIG_0` (§4) and the `1grp_msix` RDM root. Verified: both `TRIG_0` *and* `TRIG_1`
> are present for all 11 domains. `[HIGH · OBSERVED]`

### 3a. SDMA — the 4-SEngine / 2-PEB asymmetry `[HIGH · OBSERVED]`

SDMA is the largest domain: 528 USER-msix TRIG (= 264 generators), split by region:

```
APB_SE_0 / _1 / _2 / _3   :  64 TRIG each = 256  (per-SEngine USER path: 4 SE × 32 queues × 2 TRIG)
PEB_APB_IO_0 / _1         :  264 TRIG          (the PEB aggregation)
APB_IO_0                  :  8 TRIG
                            ----
                            528 USER-msix TRIG
```

The USER path exposes **all four physical SEngines** (`APB_SE_0..3`, 64 TRIG each). But the AMZN
PEB aggregation — the apex-feeding path — carries **only `_SE_0_` (128 TRIG) and `_SE_1_`
(128 TRIG)**; there is no `_SE_2_`/`_SE_3_` in the PEB SDMA names. The apex
(`peb_intc_triggers.yaml`) correspondingly has `se0_sdma_nmi[0..31]` + `se1_sdma_nmi[0..31]` = 64
bits = **2 SEngines**. Plus 8 `SDMA_D2H`/`SDMA_H2D` host-path TRIG (= 2 PEB × 2 paths × 2 TRIG).

> **GOTCHA — 4 SEngines USER, 2 SEngines PEB/apex.** A reimplementer wiring the SDMA summary
> tree must account for the 4-physical-SEngine → 2-PEB-visible-SEngine collapse: SE_2/SE_3 are
> directly host-visible (USER MSI-X) but **not** independently summarised into the PEB apex. The
> pair-aggregation mechanism (whether SE_2/SE_3 fold into SE_0/SE_1 summaries or are simply
> PEB-omitted) is **not register-encoded** — the SE_0/SE_1-only PEB coverage is the only
> observable. `[count HIGH · mechanism MED · INFERRED]`

### 3b. PCIe — four sub-domains, two carry the apex source path `[HIGH · OBSERVED]`

`PCIE_S0..S4` (5 per-SEngine slices, 16 TRIG each = 40 generators) are the bulk; `PCIE_A`
(admin, IO), `PCIE_U` (user, IO), `PCIE_M` (master, PEB) are 4 generators each. Only `PCIE_A0`
and `PCIE_M0` carry an explicit `source_path` into the apex — the **only 2 of 128** apex inputs
with a source path (`rg -c source_path peb_intc_triggers.yaml` = 2):

```
pcie_m0_nmi  ←  pcie_x8_sprot.ERRTRIG_GEN.u_errtrig.u_amzn_errtrig_prot.nmi_out
pcie_a0_nmi  ←  pcie_nmi_out
```

The `pcie_m0` path names the **AMZN (no_msix) errtrig's `nmi_out` wire** feeding the apex — the
physical AMZN `PCIE_M` generator *is* that `u_amzn_errtrig`. `[HIGH · OBSERVED]`

### 3c. The compute/memory leaves `[HIGH · OBSERVED]`

HBM / TPB / D2D / PREPROC(CC) / TOP_SP each get their USER+AMZN pair set (§3 table). The AMZN
`no_msix` `nmi_out` feeds the matching apex summary bit: `hbm_{0,1}_nmi`, `se{0,1}_tpb_nmi`,
`d2d_combined_nmi`, `cc_top_{0,1}_nmi` (the apex names the CC summaries `cc_top_0_nmi` /
`cc_top_1_nmi` → `cc_top_{0,1}_summary`), `top_sp_combined_nmi`. HBM/TPB/D2D attach under the
`SE_n` compute-tile band; TOP_SP / PCIE_A/U / D2D-subsys attach under the IO-die band.

> **CORRECTION — `cc_top_{0,1}` is the Compute Cluster, not "collective/comm".** The two apex
> CC inputs are `cc_top_0_nmi` and `cc_top_1_nmi` (`name: cc_top_{0,1}_summary`), the summaries
> of the `PREPROC` (compute-cluster) errtrig pairs — **not** a collective/communication block.
> The address-map domain token is `PREPROC`; the apex token is `cc_top`; both denote the CC.
> `[HIGH · OBSERVED · CARRIED from INT-08 correction]`

---

## 4. The PEB apex — the physical `peb_intc` `[HIGH · OBSERVED]`

The 128-input apex (the root of [`peb-cc-topsp-triggers.md`](peb-cc-topsp-triggers.md)) is **not**
an errtrig capacity pair. It is a `no_msix + msix` **flavor pair** of one 128-input `4grp`,
co-located in a `0x2000` `PEB_INTC` region and replicated on **both** PEB instances:

```
PEB_APB_IO_0_AMZN_PEB_INTC          0x02000801E090000  0x2000  (container NODE)
PEB_APB_IO_0_AMZN_PEB_INTC_TRIG_0   0x02000801E090000  0x1000  intc_4grp_no_msix  ← 128-in summary apex
PEB_APB_IO_0_AMZN_PEB_INTC_MSIX     0x02000801E091000  0x1000  intc_4grp_msix     ← host apex (MSI-X)
PEB_APB_IO_1_AMZN_PEB_INTC_TRIG_0   0x02080801E090000  0x1000  intc_4grp_no_msix
PEB_APB_IO_1_AMZN_PEB_INTC_MSIX     0x02080801E091000  0x1000  intc_4grp_msix
```

These 4 leaf nodes are the entire "apex" INTC set. The `0x…8…` nibble (`…801E…` vs `…001E…`) is
the PEB-instance select (PEB_0 / PEB_1).

- **`PEB_INTC_TRIG_0` (`no_msix`)** latches the **128 summary `nmi_out` inputs** — the 128 entries
  of `peb_intc_triggers.yaml` (128 active `trigger:` lines). Of those 128, only **2** carry a
  `source_path` (`pcie_m0`/`pcie_a0`, §3b) and **114 of 128** carry an `nmi_mask` field — leaving
  **14** unmaskable (the `pcie_m0`/`pcie_a0` pair + the 12 `se0_sdma_nmi[0..11]` head; the
  always-on criticals). `[HIGH · OBSERVED]`
- **`PEB_INTC_MSIX` (`msix`)** @ `+0x1000` delivers the rolled-up apex interrupt to the management
  core via MSI-X — the host-delivery twin of the same 128 inputs.

> **QUIRK — flavor pair, not capacity pair.** The apex `TRIG_0` is the `no_msix` summary half;
> its twin is `PEB_INTC_MSIX`, **not** a `PEB_INTC_TRIG_1`. There is no `TRIG_1` for the apex
> (`rg -c PEB_INTC_TRIG_1` = 0). Do not read the apex's `TRIG_0` suffix as half of a
> `trig_0/trig_1` 256-cap pair — it is one 128-input `4grp` exposed in two flavors, replicated
> per PEB. `[HIGH · OBSERVED]`

### 4a. The `IO_INTC_RDM` root (the 4 `1grp_msix`) `[HIGH · OBSERVED]`

The four `intc_1grp_msix` instances are the RDM (interrupt Root-Domain) MSIX root, one per
`APB_IO` / `PEB_APB_IO` at the verbatim `0x…6C82000` offset:

```
APB_IO_0_USER_IO_INTC_RDM_MSIX        0x000008006C82000  intc_1grp_msix
APB_IO_1_USER_IO_INTC_RDM_MSIX        0x000808006C82000  intc_1grp_msix
PEB_APB_IO_0_USER_IO_INTC_RDM_MSIX    0x020008006C82000  intc_1grp_msix
PEB_APB_IO_1_USER_IO_INTC_RDM_MSIX    0x020808006C82000  intc_1grp_msix
```

Each is a 32-input (1-group) host-facing MSIX with the **128-entry over-provisioned table**
(`INTC_NUM_GROUPS=1` but `NUM_OF_TRIGS=128` — verified in the schema, see
[`intc-1group-apintc.md`](../csr/intc-1group-apintc.md) §3a). The RDM fabric master owns *both*
a `1grp_msix` root **and** a full errtrig pair (the `INTC_RDM` domain in §3, 4 USER + 2 AMZN
generators). `[HIGH · OBSERVED]`

---

## 5. The physical cascade — leaf → io-fabric → PEB-apex → Q7 `[chain HIGH · final hop INFERRED]`

The cascade assembled from the physical bases (§1/§3/§4), the severity-wire model
([`intc-4group.md`](../csr/intc-4group.md) §5a), and the apex source paths. One SDMA example with
real bases:

```
L0  SOURCE EVENT
      SDMA notific wr-buffer-full  →  notific_1_queue status bit
      @ APB_SE_0_USER_FIS_SDMA_0_FIS_0_USER_ERRTRIG_NOTIFIC  0x100C002000

L1  LEAF ERRTRIG intc PAIR  (the physical latch)
      USER (host leaf, msix):
        …_USER_ERRTRIG_TRIG_0  0x100C000000  intc_4grp_msix   ──MSI-X──▶ PCIe host (DIRECT)
        …_USER_ERRTRIG_TRIG_1  0x100C001000  intc_4grp_msix
      AMZN (on-die summary, no_msix):
        PEB_APB_IO_0_AMZN_SE_0_SDMA_0_…_ERRTRIG_TRIG_0  intc_4grp_no_msix
          → int_cause → 4 severity wire-ORs (error/abort/fatal/log) + Mask_msi_x → one nmi_out

L2  IO-FABRIC / RDM intermediate  (IO-die aggregation)
      APB_IO_n / PEB_APB_IO_n errtrigs (PCIE_A/U, D2D, TOP_SP, INTC_RDM) +
      IO_INTC_RDM_MSIX root (0x…6C82000). The 640 BCAST aliases live here.
      AMZN no_msix nmi_out wires converge toward the PEB.

L3  PEB APEX  (the physical peb_intc)
      PEB_APB_IO_0_AMZN_PEB_INTC_TRIG_0  0x02000801E090000  intc_4grp_no_msix
        → latches the 128 summary nmi_out inputs (the 128-bit apex bus)
      PEB_APB_IO_0_AMZN_PEB_INTC_MSIX    0x02000801E091000  intc_4grp_msix
        → MSI-X to the management core         (replicated on PEB_1 @ 0x…801E09x000)

L4  DELIVERY to the Q7 ("Pacific") management core IRQ and/or a GIC.   [INFERRED]
      Not register-encoded. A GIC is confirmed to exist (CXELA500 ELA), but the
      apex-pending-bit → Q7/GIC-vector map is firmware/HW-owned.
```

The resolution of a single source event up to the apex, in C (the address arithmetic is byte-exact
from the schemas; the wire-OR fan-in is name-implied):

```c
/* All offsets/strides byte-exact from intc_4grp_*_unit.json (see intc-4group.md).
 * The leaf→apex *wire* fan-in is name-implied (MED·INFERRED); the address math is HIGH·OBSERVED. */

#define INTC_GRP_STRIDE     0x40u     /* ctrl.BundleSizeInBytes (hex)                */
#define INTC_NUM_GROUPS     4u        /* 4grp; =1 for the 1grp RDM root              */
#define ERRTRIG_TRIG0_OFF   0x0000u   /* container +0x0                              */
#define ERRTRIG_TRIG1_OFF   0x1000u   /* container +0x1000                          */
#define ERRTRIG_NOTIFIC_OFF 0x2000u   /* container +0x2000 (notific_1_queue)        */
#define INTC_CAUSE_OFF      0x00u     /* per-group: int_cause_grp                    */
#define INTC_MASK_OFF       0x10u     /* per-group: int_mask_grp (rst 0xffffffff)    */
#define INTC_ERR_MSK_OFF    0x2Cu     /* per-group: int_error_msk_grp                */
#define INTC_ABORT_MSK_OFF  0x30u     /* int_abort_msk_grp (RW no_msix / RO msix)    */
#define INTC_FATAL_MSK_OFF  0x34u
#define INTC_LOG_MSK_OFF    0x38u

/* A 128-input 4grp unit's per-group register address. */
static inline uint64_t intc_grp_reg(uint64_t unit_base, unsigned grp, unsigned reg_off) {
    return unit_base + (uint64_t)grp * INTC_GRP_STRIDE + reg_off;   /* grp ∈ 0..3, bit b ⇒ g*32+b */
}

/* Severity wire-OR for one group: Severity = OR(Cause & !Mask). Four independent lines. */
static inline uint32_t intc_grp_severity(uint64_t unit_base, unsigned grp, unsigned sev_msk_off) {
    uint32_t cause = mmio_rd32(intc_grp_reg(unit_base, grp, INTC_CAUSE_OFF));
    uint32_t msk   = mmio_rd32(intc_grp_reg(unit_base, grp, sev_msk_off));
    return cause & ~msk;                              /* nonzero ⇒ this severity line asserts */
}

/* Resolve a leaf no_msix aggregator's summary "nmi_out" → which apex bit it drives.
 * The aggregator's nmi_out is the OR of all four severity lines across its 4 groups.
 * The leaf→apex bit binding lives in peb_intc_triggers.yaml file order, NOT in any
 * register — so this returns the *assertion*; the apex bit index is YAML-owned (MED·INFERRED). */
static bool leaf_nmi_out_asserts(uint64_t leaf_no_msix_base) {
    uint32_t any = 0;
    for (unsigned g = 0; g < INTC_NUM_GROUPS; ++g) {
        any |= intc_grp_severity(leaf_no_msix_base, g, INTC_ERR_MSK_OFF);
        any |= intc_grp_severity(leaf_no_msix_base, g, INTC_ABORT_MSK_OFF);
        any |= intc_grp_severity(leaf_no_msix_base, g, INTC_FATAL_MSK_OFF);
        any |= intc_grp_severity(leaf_no_msix_base, g, INTC_LOG_MSK_OFF);
    }
    return any != 0;                                  /* drives one of the 128 apex inputs */
}

/* The apex (PEB_INTC_TRIG_0, no_msix) latches all 128 summary nmi_out wires. Its own
 * nmi_out, in turn, is delivered host-side by its msix twin (PEB_INTC_MSIX). The final
 * apex→Q7/GIC vector hop is firmware-owned and is NOT modeled here.            [INFERRED] */
static bool apex_pending(uint64_t apex_trig0_base /* no_msix */) {
    return leaf_nmi_out_asserts(apex_trig0_base);     /* 128-input bus already merged in HW */
}
```

---

## 6. Instance-count reconciliation — the full 1,932 audit `[HIGH · OBSERVED]`

```
TOTAL intc instances (json: csrs/intc/*)            : 1,932
  by schema :  4grp_no_msix  1,070 |  4grp_msix  858 |  1grp_msix  4
  by region :  APB_SE (4 SEngines)  256   (all SDMA USER/msix per-SEngine path)
               APB_IO_*             174
               PEB_APB_IO_* (direct) 862
               PEB_APB_IO_BCAST_*   640   (broadcast aliases)        256+174+862+640 = 1932 ✓
  by side   :  USER  860  (856 errtrig-msix + 4 1grp-msix RDM root)
               AMZN 1072  (1068 errtrig-no_msix + 2 apex-no_msix + 2 apex-msix)

STRUCTURAL DECOMPOSITION (sums to 1,932):
    640   BCAST broadcast-aperture ALIASES (all no_msix; PCIe/D2D only)
  1,284   DIRECT errtrig TRIG_0/TRIG_1   = 642 generator pairs (856 USER/msix + 428 AMZN/no_msix)
      4   PEB APEX                       = 2 PEB × {no_msix TRIG_0 + msix MSIX}
      4   IO_INTC_RDM root               = 1 per APB_IO / PEB_APB_IO  (1grp_msix)
  -----
  1,932   TOTAL
```

Cross-checks (all PASS): vs [`intc-4group.md`](../csr/intc-4group.md) §8 schema binding
(1,070 / 858 / 4) ✓; vs [`errtrig-fis-routing.md`](errtrig-fis-routing.md) USER→msix 856 /
AMZN→no_msix 1068 ✓; vs the [`intc-4group.md`](../csr/intc-4group.md) §8 keyword counts
(sdma 528, pcie 104, d2d 64, tpb 32, hbm 16, preproc 16, top_sp 80) ✓.

> **GOTCHA — 640 BCAST instances are address aliases, not silicon.** The 1,932 count includes
> 640 broadcast-aperture nodes (all `no_msix`, PCIe/D2D only) that view the same FIS silicon over
> the privileged-APB BCAST fabric. A BCAST node and its direct twin share the offset suffix but
> sit in different address bands — e.g.
> `PEB_APB_IO_BCAST_0_0_AMZN_IO_PCIE_A_…_TRIG_0 @ 0x020008036002000` vs the direct
> `PEB_APB_IO_0_AMZN_IO_PCIE_A_…_TRIG_0 @ 0x020008016002000` (Δ = **+0x20000000**; `…002000`
> suffix identical). The 640 split into **16 BCAST groups** (`BCAST_{0,1}_{0..7}`), each exactly
> **40 nodes** = 20 targets × 2 TRIG (per group: D2D 8 + PCIE_A 1 + PCIE_U 1 + PCIE_S·SE_0 5 +
> PCIE_S·SE_1 5), bands stepping +0x20000000 each. By domain: D2D 256 + PCIE_S 320 (SE_0 160 +
> SE_1 160; SE_2/SE_3 = 0) + PCIE_A 32 + PCIE_U 32 = 640 — **only** the high-replication IO-die
> PCIe/D2D domains are broadcast; no SDMA/TPB/HBM/PREPROC/TOP_SP. Any "physical controller count"
> must **de-alias** these → ~**642 direct errtrig generators** + the apex + the RDM root.
> `[count + band-delta HIGH · OBSERVED; alias-targets-same-silicon MED · INFERRED — the differing
> bands mean the 1:1 mapping is reasoned, not byte-traced.]`

---

## 7. Cross-generation divergence — Maverick `[schema header HIGH · OBSERVED; v5 interior INFERRED]`

The Maverick (v5) INTC architecture **diverges substantially**. The shipped Maverick `csrs/`
schema headers are read directly (header-OBSERVED); the v5 instance population is from the
`al_address_map_db` (streamed as JSON, never `pickle.load`-ed). **v5-interior behavior is
INFERRED** — only the schema headers and record counts are observed.

**7a. A decentralized per-IP-block INTC layer.** Maverick reserves `type='INTC'` for a *new*
family of per-block embedded controllers (`trfc_gen` / `fci` / `pmu` / `dge` descriptor-push +
worker / `d2d_tl` / `d2d_ll_phy` / `udma` primary+secondary / `fcm`-crypto / `dma_landing_buffer`
/ `axi_wr`+`rd`_term) — **13 schemas, 5,904 records, sum byte-exact** (see the NOTE below).
Cayman has **zero** of these — its only aggregation primitive is the errtrig pair. The records
cluster on the C_DIE (`C_DIE` 4,528 / `H_DIE` 1,376 / `IO_DIE` 0 for the `type='INTC'` family).
This is a fundamentally different (decentralized) interrupt fabric.
`[record counts HIGH · OBSERVED (streamed JSON); v5-interior behavior INFERRED]`

**7b. The Cayman-lineage errtrig primitive survives, REGFILE-typed.** The Cayman `intc_4grp`
units still exist in Maverick — but classified `type='REGFILE'`, not `type='INTC'` (the pkl
reserves `INTC` for the new per-block family). The errtrig PAIR is frozen across generations; what
changed is the **addition** of a per-block INTC layer on top, not the errtrig.
`[CARRIED · v5-INFERRED interior]`

**7c. The security 8-group IOFIC (header-OBSERVED).** Maverick adds
`ap_intc_8grp_msix_unit` — verified in the Maverick `csrs/ap_intc/`: `HalName "iofic_x8_msix"`,
`Type "NODE"`, `SizeInBytes "0x2000"`, `InterfaceType APB`. It is an 8-group (256-input),
MSIX-capable IOFIC — a width *and* MSI-X capability Cayman's `iofic_x{1,2,4}` (MEM, no MSI-X)
never had. In the pkl it appears as **336 `type='NODE'` records** (`short_name INTC_BASE`), all on
the DMA app path (`…USER_DDMA_n_UDMA_APP_GEN_INT_CTRL_…_INTC_BASE`) and all on the C_DIE. The
Maverick `ap_intc_grp_ctrl` is grown to **12 registers** (vs Cayman's 9) and is **APB-interfaced**
(vs Cayman's MEM/NONE), adding `int_sec_grp` + `int_regs_sec_grp` — the SWOM write-lock security
fork (see [`intc-1group-apintc.md`](../csr/intc-1group-apintc.md) §7).
`[header + record counts HIGH · OBSERVED; silicon behavior v5-INFERRED]`

**7d. Per-die explicit apex.** Maverick replicates the apex per SEngine × per die: the pkl carries
**12 `PEB_INTC` NODE records** (`short_name PEB_INTC`, schema `address_map/INTC.json`) =
4 SEngines × 3 dies (`SECURE_INT_SENG_{0..3}_{C_DIE,H_DIE,IO_DIE}_PEB_INTC`), vs Cayman's
single-view 2-PEB apex (§4). All three dies are present at this NODE level. The
C_DIE/H_DIE/IO_DIE split (the multi-die package) makes the apex a per-die aggregator.
`[record counts HIGH · OBSERVED; v5-INFERRED interior]`

> **NOTE — the two-metric cross-gen contrast.** Cayman is the centralized model:
> **1,070 + 858 + 4 = 1,932** instances over **3** INTC schemas. Maverick is decentralized: the
> pkl census records **5,904** `type='INTC'` records, summing byte-exact across the **13** new
> per-block schemas (`trfc_gen` 1136 + `fci` 672 + `pmu` 648 + `dge`-desc-push 576 + `d2d_tl`
> 440 + `d2d_ll_phy` 440 + `dge`-worker 408 + `udma`-primary 336 + `udma`-secondary 336 + `fcm`
> 312 + `dma_landing_buffer` 312 + `axi_wr_term` 144 + `axi_rd_term` 144 = 5,904). The
> Cayman-lineage errtrig units survive as `type='REGFILE'` — **1,880** `intc_4grp_no_msix` +
> **912** `intc_4grp_msix` (+ **2,200** `ap_intc_4grp`). The Cayman figures are byte-OBSERVED
> here; the **Maverick record counts are OBSERVED in the streamed JSON but the v5 *interior*
> behavior is INFERRED** (the bases are Maverick-specific; the schema-family divergence is the
> generation-general finding). `[Cayman HIGH · OBSERVED; Maverick schema-counts OBSERVED ·
> v5-interior INFERRED]`

> **CORRECTION — "1,372" is the errtrig anchor-NODE total, not a symmetric `TRIG` count.** The
> v5 figure sometimes paired with 5,904 is **1,372**, and it is **not** `TRIG_0 = TRIG_1 = 1,372`
> (those are 844 / 844 in the DB). Streaming the pkl-JSON shows 1,372 = the errtrig *generator
> anchor* NODEs = `errtrig_amzn.json` (**928**, `short_name AMZN_ERRTRIG`) + `errtrig_user.json`
> (**444**, `short_name USER_ERRTRIG`); 928 + 444 = 1,372. These are the parent NODEs that *own*
> the `TRIG_0`/`TRIG_1` INTC children and carry no `TRIG_n` suffix themselves. So the cross-gen
> pair is **(5,904 `type='INTC'` per-block instances) / (1,372 errtrig anchor NODEs = 928 AMZN +
> 444 USER)**, replacing the imprecise "symmetric PAIR" framing. `[v5 record-counts HIGH ·
> OBSERVED (streamed JSON); v5 silicon behavior INFERRED]`

---

## 8. Where INTC sits behind the security remapper `[HIGH · OBSERVED]`

The errtrig fabric splits along the same USER/AMZN trust boundary the `sprot` remapper enforces.
Read byte-exact from `csrs/sprot/{amzn,user}_remapper.json`:

| remapper | `pass_on_miss` reset | posture | which INTC side |
|---|---|---|---|
| `amzn_cam_pass_on_miss` (rd + wr) | **`0x0`** | **fail-CLOSED** | the AMZN `no_msix` aggregators / apex |
| `user_cam_pass_on_miss` (rd + wr) | **`0x1`** | **fail-OPEN** | the USER `msix` host leaves |

So a CAM miss on the **AMZN** (privileged, on-die-summary) perimeter denies by default
(`pass_on_miss = 0`), while a miss on the **USER** (host-reachable) perimeter passes through
(`pass_on_miss = 1`). The privileged summary/apex path is the hardened one; the host leaf path is
permissive. `[HIGH · OBSERVED]` (full treatment in
[`../csr/remapper.md`](../csr/remapper.md) / [`../address/pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md).)

---

## 9. Reimplementer's checklist `[HIGH · OBSERVED unless noted]`

1. Lay down INTC as the **errtrig PAIR primitive**: per per-block aggregation, a `0x3000`
   container = `TRIG_0 @+0x0` + `TRIG_1 @+0x1000` (both `intc_4grp`, 128-in each) + `NOTIFIC
   @+0x2000` (`notific_1_queue`). Every FIS-attached domain gets a pair — **no** single-`intc`
   shortcut for CC/TOP_SP.
2. Choose flavor by **role, not domain**: USER/host-reachable → `intc_4grp_msix` (delivers MSI-X
   to the PCIe host); AMZN/on-die → `intc_4grp_no_msix` (emits 4 severity wire-ORs + `Mask_msi_x`
   summary upward). 856 msix + 1068 no_msix errtrig TRIG.
3. The **apex** is `peb_intc`: one 128-in `4grp` exposed as `{no_msix TRIG_0 + msix MSIX}`,
   replicated per PEB (2). **No `TRIG_1`.** Wire the leaf `no_msix` `nmi_out` summaries into its
   128-bit bus; 114 of 128 inputs maskable, 2 carry an explicit `source_path`.
4. Place exactly **4** `intc_1grp_msix` RDM roots at `0x…6C82000`; size their MSI-X table at
   **128 entries** despite only 32 live cause bits (`NUM_OF_TRIGS=128` is a template constant).
5. Add **640 BCAST aliases** (`no_msix`, PCIe/D2D only) as a distinct address band over the same
   FIS silicon — de-alias before counting physical controllers (~642 direct generators).
6. Account for the SDMA **4-SEngine USER vs 2-SEngine PEB** asymmetry: expose all 4 SEngines to
   USER MSI-X, but only SE_0/SE_1 into the apex summary.
7. The apex `msix` → Q7/GIC delivery is **`[INFERRED]`**: a GIC exists (CXELA500 ELA), but the
   vector map is firmware-owned.
8. For a **v5** target, add the Maverick layers: per-IP-block `type='INTC'` controllers, the
   `iofic_x8_msix` security IOFIC (`int_sec_grp`/`int_regs_sec_grp` SWOM), and the per-die apex —
   all header-OBSERVED, interior INFERRED.

---

## 10. Confidence ledger

**`[HIGH · OBSERVED]`** — the 1,932 census and the 1,070 / 858 / 4 schema split; `1grp_no_msix`
and `ap_intc` = 0 placed; the USER→msix 856 / AMZN→no_msix 1068 errtrig split; every domain
instantiating a `TRIG_0+TRIG_1` pair (642 direct generators) and the per-domain TRIG table (§3);
the `0x3000` errtrig container layout (`TRIG_0 @+0x0` / `TRIG_1 @+0x1000` / `NOTIFIC @+0x2000`,
SDMA_0 verbatim); the 4-node PEB apex with verbatim bases (`0x…01E090000` / `…091000`) and the
absence of `PEB_INTC_TRIG_1`; the 128-active / 114-nmi_mask / 2-source_path apex; the 4
`IO_INTC_RDM_MSIX` roots at `0x…6C82000`; the 640 BCAST = PCIe/D2D-only / all-no_msix with the
band example; the SDMA 4-SE-USER / 2-SE-PEB split + D2H/H2D paths; the `cc_top` = CC naming; the
`sprot` `amzn`=0x0 / `user`=0x1 `pass_on_miss`; the Maverick `iofic_x8_msix` + 12-reg APB
`ap_intc_grp_ctrl` headers.

**`[MED · INFERRED]`** — the leaf → io-fabric → apex *wire* fan-in (name-implied, not
register-traced); the BCAST-alias → same-silicon 1:1 mapping (differing bands); the
4-SE → 2-PEB pair-aggregation mechanism; the both-PEB-latch-same-128 assumption.

Maverick (streamed pkl-JSON, OBSERVED record counts; v5-interior behavior INFERRED): the 5,904
`type='INTC'` records summing byte-exact over 13 per-block schemas; the 1,880 / 912 / 2,200
`type='REGFILE'` intc_4grp/ap_intc_4grp; the 336 `ap_intc_8grp_msix` NODE records (all C_DIE);
the 12 per-die `PEB_INTC` NODEs; the 1,372 errtrig anchor NODEs = 928 `errtrig_amzn` + 444
`errtrig_user` (NOT a symmetric `TRIG` count).

**`[CARRIED]`** — the per-group register semantics + 4 severity wire-ORs
([`intc-4group.md`](../csr/intc-4group.md)); the errtrig generator + notific backing
([`errtrig-fis-routing.md`](errtrig-fis-routing.md)); the apex 128-bit map
([`peb-cc-topsp-triggers.md`](peb-cc-topsp-triggers.md)).

**`[LOW · OPEN]`** — the final apex-MSIX → Q7/GIC vector hop (firmware/HW-owned, in no shipped
artifact); the v5 interior silicon behavior behind the Maverick header-OBSERVED schemas.
