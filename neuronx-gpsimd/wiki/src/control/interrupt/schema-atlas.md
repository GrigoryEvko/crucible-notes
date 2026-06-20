# Trigger-YAML Schema Atlas + Source Map

This is the **synthesis capstone** of the interrupt sub-section. Every other page on
this floor enumerates one *domain's* trigger sources; this page steps back and asks
the orthogonal structural question: **how many distinct `*_triggers.yaml` schema
SHAPES exist, which sources use each, and how does every source thread from its leaf
schema → its routing leaf → the apex → delivery?** It is the single reference a
control-plane reimplementer opens first: a parser written against the wrong schema
silently mis-reads an entire domain (the [TPB `msix_mask`](pcie-hbm-tpb-d2d-triggers.md)
trap, the [HBM edge/level inversion](pcie-hbm-tpb-d2d-triggers.md), the
[apex `source_path`-on-only-2](peb-cc-topsp-triggers.md) trap), so the *taxonomy* is
the deliverable, not any one count.

The two halves:

1. **The SCHEMA ATLAS (§1–§4)** — every distinct per-entry key-set shape across the
   eleven shipped Cayman trigger tables, reduced to **six base families** plus the
   apex's four sub-shapes; the per-key presence matrix; the parse/normalize algorithm.
2. **The SOURCE MAP (§5–§7)** — every trigger source family → its schema → its routing
   leaf → its apex roll-up → delivery, as one continuous table and cascade.

It does **not** re-derive any source from scratch — every count, offset, and key is
already byte-grounded on a committed sibling. This page reconciles the eleven tables'
*shapes* into one atlas and re-verifies each NEW schema-shape claim against the
specific YAML by absolute path. The committed siblings synthesized here:
[`sdma-triggers.md`](sdma-triggers.md) · [`io-fabric-triggers.md`](io-fabric-triggers.md) ·
[`pcie-hbm-tpb-d2d-triggers.md`](pcie-hbm-tpb-d2d-triggers.md) ·
[`peb-cc-topsp-triggers.md`](peb-cc-topsp-triggers.md) ·
[`physical-intc-instances.md`](physical-intc-instances.md) ·
[`errtrig-fis-routing.md`](errtrig-fis-routing.md) ·
[`nsm-flow-unified.md`](nsm-flow-unified.md).

> **PROVENANCE.** Every key-set, count, and per-key presence figure below was
> re-derived this session directly from the shipped, RTL-generated **Cayman**
> trigger YAMLs in `…/cayman-arch-regs_tgz/intc/*_triggers.yaml` (`yq` over each
> file by absolute path, never a directory scan). The Cayman generation is
> byte-grounded; **v5 / Maverick is header-OBSERVED only** (§8) — every v5-interior
> claim is flagged INFERRED. The final apex → Q7/GIC vector hop is firmware/HW-owned
> and in no shipped artifact — an explicit non-claim (§7). All output is derived from
> static analysis of the shipped descriptor artifacts only.

> **Confidence legend.** `HIGH` = literal value read from the YAML / re-counted by
> `yq`. `MED` = inference from naming + cross-reference. `LOW` = plausible, flagged.
> `OBSERVED` = read from a shipped file. `INFERRED` = reasoned from corroboration.
> `CARRIED` = forwarded from a named sibling, not re-derived here.

---

## 0. The eleven tables at a glance `[HIGH · OBSERVED]`

Eleven `*_triggers.yaml` ship in the Cayman `intc/` directory. Their counts and
schema families, every count re-run this session with `yq 'length'` /
`yq '[.[]|keys[]]|unique'` against the file by absolute path:

| Table | `yq 'length'` | Key-union | Schema family (§1) | edge / level |
|-------|--------------:|----------:|--------------------|-------------:|
| `sdma_triggers.yaml` | **254** | 5 | **MINIMAL-5** | 223 / 31 |
| `io_fabric_triggers.yaml` | **243** | 5 | **MINIMAL-5** | 226 / 17 |
| `pcie_triggers.yaml` | **228** | 9 | **RICH-9** (`nmi_*`+`source_*`) | 216 / 12 |
| `hbm_triggers.yaml` | **223** | 9 | **RICH-9** (`nmi_*`+`source_*`) | 31 / 192 |
| `tpb_triggers.yaml` | **216** | 9 | **TPB-9** (`msix_mask`+`tog2pul_only`) | 158 / 58 |
| `d2d_triggers.yaml` | **216** | 5 | **MINIMAL-5** | 67 / 149 |
| `cc_triggers.yaml` | **98** | 8 | **CC-8** (`source_*`+`msix_mask`) | 92 / 6 |
| `top_sp_triggers.yaml` | **82** | 7 | **TOPSP-7** (min5 + 2 `nmi_*` rows) | 74 / 8 |
| `peb_intc_triggers.yaml` | **128** | 10 | **APEX-10** (4 sub-shapes) | 24 / 104 |

Two tables are *not* domain leaves: `peb_intc_triggers.yaml` is the **apex** (rolls
up all the others), and `io_fabric_triggers.yaml` is the IO-die aggregator. Mariana /
Mariana+ / Tonga ship **no** trigger-YAML directory at all; only Cayman, Sunda, and
Maverick do. `[HIGH · OBSERVED]`

> **KEYSTONE — six base families, one parser per family.** The eleven tables reduce
> to **six** distinct base key-set shapes (MINIMAL-5, RICH-9, TPB-9, CC-8, TOPSP-7,
> APEX-10). A control plane must dispatch its YAML parser by **detecting the family**,
> not by assuming a shared schema. Three keys — `nmi_mask`, `msix_mask`,
> `tog2pul_only` — are mutually-exclusive family discriminators (§1.1). `[HIGH · OBSERVED]`

---

## 1. The SCHEMA ATLAS — the six base families `[HIGH · OBSERVED]`

Every per-entry mapping in every table is drawn from a **closed alphabet of 12 keys**.
No table invents a key outside this set; the families differ only in *which subset* of
the 12 each entry carries. The 12 keys, with the family that introduces each:

| key | type | introduced by | semantics |
|-----|------|---------------|-----------|
| `trigger` | `bus[bit]` string | **all** | the HW trigger-bus signal identity (`name`'s raw form, keeps `[N]`/`[N][M]` brackets). YAML order == INTC bit order. |
| `name` | string | **all** | the de-indexed legal identifier (brackets → underscore, for some groups). |
| `description` | string \| `null` | **all** | free-text cause (some `null` / placeholder; §6 typo ledger). |
| `edge_triggered` | bool | **all** | edge(1)/level(0) → realizes `int_posedge_grp` (§5.2). |
| `needs_cdc` | bool \| `null` | **all-but-apex-summaries** | "must this source cross a clock domain into the INTC?" |
| `source_clock` | string | RICH-9 / TPB-9 / CC-8 | the INTC-side **destination** clock the CDC sampler reads the source into. |
| `source_reset_n` | string | RICH-9 / TPB-9 / CC-8 | mirrors `source_clock` (the reset of that destination domain). |
| `nmi_mask` | int(0/1) | RICH-9 / TOPSP-7 / APEX | NMI-summary mask (apex-rollup form). |
| `nmi_msix_mask` | int(0/1) | RICH-9 / TOPSP-7 / APEX | co-occurs with `nmi_mask`. |
| `msix_mask` | int(0) | TPB-9 / CC-8 | per-leaf MSI-X mask. **Absent everywhere else** — refutes "msix_mask never present". |
| `tog2pul_only` | bool(true) | **TPB-9 only** | toggle-to-pulse edge-detect path (replaces the simple CDC sampler). |
| `critical` | int(1) | **APEX only** | the SoC-survival fast-path flag ("*not a summary*"). |
| `source_block` / `source_path` | string | **APEX shape-C only** | names the leaf `nmi_out` wire (only `pcie_m0`/`pcie_a0`). |

### 1.1 The schema-variation matrix — the central insight `[HIGH · OBSERVED]`

Per-key **presence count** across all eleven tables, every cell re-run this session
(`yq '[.[]|select(has("KEY"))]|length'` against the file by absolute path). A `—`
means the key is absent on **every** entry; a number is the count of entries carrying
it (out of the table's `length`):

| key | SDMA 254 | IOF 243 | PCIe 228 | HBM 223 | TPB 216 | D2D 216 | CC 98 | TOPSP 82 | APEX 128 |
|-----|:-------:|:------:|:-------:|:------:|:------:|:------:|:----:|:-------:|:-------:|
| `trigger` | 254 | 243 | 228 | 223 | 216 | 216 | 98 | 82 | 128 |
| `name` | 254 | 243 | 228 | 223 | 216 | 216 | 98 | 82 | 128 |
| `description` | 254 | 243 | 228 | 223 | 216 | 216 | 98 | 82 | 128 |
| `edge_triggered` | 254 | 243 | 228 | 223 | 216 | 216 | 98 | 82 | 128 |
| `needs_cdc` | 254 | 243 | **212** | 223 | 216 | **67** | 98 | **80** | **14** |
| `source_clock` | — | — | 95 | 196 | 126 | — | 31 | — | — |
| `source_reset_n` | — | — | 95 | 196 | 126 | — | 31 | — | — |
| `nmi_mask` | — | — | **149** | 223 | — | — | — | **2** | **114** |
| `nmi_msix_mask` | — | — | 149 | 223 | — | — | — | 2 | 114 |
| `msix_mask` | — | — | — | — | **131** | — | **98** | — | — |
| `tog2pul_only` | — | — | — | — | **4** | — | — | — | — |
| `critical` | — | — | — | — | — | — | — | — | **32** |
| `source_path` | — | — | — | — | — | — | — | — | **2** |

The **three discriminator keys** that pick the family by mere presence:

* **`nmi_mask`** present ⇒ the table participates in NMI-summary rollup → RICH-9
  (PCIe/HBM, on every entry), or TOPSP-7 (only the 2 ERG-ECC rows), or APEX.
* **`msix_mask`** present ⇒ TPB-9 (131 entries) or CC-8 (all 98). Mutually exclusive
  with `nmi_mask` — **no table carries both.**
* **`tog2pul_only`** present ⇒ **TPB only** (4 entries) — a hard fingerprint for the
  TPB table.

> **QUIRK — `nmi_mask` and `msix_mask` are mutually exclusive (H-ATLAS-1).** No single
> entry in any of the eleven tables carries both `nmi_mask` and `msix_mask`
> (re-verified: every table with `nmi_mask>0` has `msix_mask==—`, and vice-versa).
> The two masks are the **two delivery axes** — `nmi_mask` is the on-die NMI-summary
> rollup (the AMZN/`no_msix` path), `msix_mask` is the per-leaf MSI-X (the USER/`msix`
> path). A leaf table is stamped for exactly one. Their presence is the cleanest
> single-bit family discriminator a parser can key on. `[HIGH · OBSERVED]`

> **GOTCHA — `needs_cdc` is partial in only two tables (PCIe 212/228, D2D 67/216).**
> In the other nine tables `needs_cdc` is present on **every** entry; in PCIe it is
> *absent* on the 16 `reset_handshake_intr` (already in the INTC domain) and in D2D on
> idx 67..215 (the 148 D2D-subsystem sources, whose verbatim comments state "CDC
> happens at controller/MPCS … so no CDC needed here"). A parser must treat the
> *absence* of `needs_cdc` as "no per-trigger CDC metadata", not as a parse error.
> `[HIGH · OBSERVED]`

### 1.2 The six base families — keyset + members `[HIGH · OBSERVED]`

| Family | Keyset (verbatim union) | size | Tables (sources) | discriminator |
|--------|-------------------------|-----:|------------------|---------------|
| **MINIMAL-5** | `{trigger, name, description, needs_cdc, edge_triggered}` | 5 | SDMA (254), io-fabric (243), D2D (216), TOP_SP baseline (80 of 82) | no mask keys |
| **RICH-9** | MINIMAL-5 + `{source_clock, source_reset_n, nmi_mask, nmi_msix_mask}` | 9 | PCIe (228), HBM (223) | `nmi_mask` on **every** entry |
| **TPB-9** | MINIMAL-5 + `{source_clock, source_reset_n, msix_mask, tog2pul_only}` | 9 | TPB (216) | `tog2pul_only` present |
| **CC-8** | MINIMAL-5 + `{source_clock, source_reset_n, msix_mask}` | 8 | CC (98) | `msix_mask` but **no** `tog2pul_only` |
| **TOPSP-7** | MINIMAL-5 + `{nmi_mask, nmi_msix_mask}` (only the 2 ERG-ECC rows) | 7 | TOP_SP (82) | `nmi_mask` on **exactly 2** rows |
| **APEX-10** | `{trigger, name, description, edge_triggered, needs_cdc, nmi_mask, nmi_msix_mask, source_block, source_path, critical}` | 10 | PEB apex (128) | `critical` present (32) |

Two structural relationships make the family lattice clean:

* **TPB-9 and CC-8 differ by exactly one key** (`tog2pul_only`). CC is TPB's POOL
  Q7-cluster IP re-instanced as the 4-core PREPROC compute cluster (its banner is even
  the copy-pasted `# Cayman TPB Triggers`; [CC §5](peb-cc-topsp-triggers.md)), so it
  inherits TPB-9 minus the 4 PE-array `fp_state_*_array_summary` toggle-to-pulse
  sources that need `tog2pul_only`. The shared lineage is *visible in the schema*.
* **RICH-9 and TPB-9 are both 9-key but DISJOINT in their extra-4**: RICH-9 adds
  `{nmi_mask, nmi_msix_mask}` (rollup), TPB-9 adds `{msix_mask, tog2pul_only}`
  (per-leaf MSI-X + edge-detect). Same arity, different shape — the count alone is not
  the family. `[HIGH · OBSERVED]`

> **CORRECTION — TOP_SP is NOT a sixth one-row mask family; it is MINIMAL-5 with a
> 2-row ECC variant.** 80 of TOP_SP's 82 entries are pure MINIMAL-5; only the 2
> ERG-ECC rows (`intr_trig_{uncerr,corerr}`, idx 1–2) drop `needs_cdc` and add
> `nmi_mask`/`nmi_msix_mask` (both 0) to feed the leaf's nmi-summary. Re-verified:
> `yq '[.[]|select(has("nmi_mask"))]|length'` = **2**,
> `yq '[.[]|select(has("source_clock"))]|length'` = **0**. So TOPSP-7 is a *2-entry
> local variant inside a MINIMAL-5 table*, **not** the TPB `msix_mask`/`tog2pul_only`
> variant and not a sixth wholesale family. A parser keying TOP_SP on "MINIMAL-5"
> must tolerate the 2 nmi-bearing rows. `[HIGH · OBSERVED]`

### 1.3 The apex's four sub-shapes (APEX-10 decomposed) `[HIGH · OBSERVED]`

The apex `peb_intc_triggers.yaml` is the only table whose **128 entries span four
distinct sub-keysets** drawn from the 10-key union. Re-counted this session
(`yq` predicates by absolute path):

| Sub-shape | Keyset | Count | members | `yq` predicate |
|-----------|--------|------:|---------|----------------|
| **A** | `{description, edge_triggered, name, nmi_mask, nmi_msix_mask, trigger}` | **80** | generic per-domain summaries | `has(nmi_mask) − B − C` |
| **B** | A + `critical` | **32** | critical fast-path (§5.3) | `select(.critical==1)` |
| **C** | A + `{source_block, source_path}` | **2** | `pcie_m0`/`pcie_a0` (the only named leaf wires) | `select(has(source_path))` |
| **D** | `{description, edge_triggered, name, needs_cdc, trigger}` | **14** | FIS sprot/cntrl + SE apbblk (no nmi/crit) | `select(has(needs_cdc))` |

Verified arithmetic: `nmi_mask`-bearing = 114 = A(80)+B(32)+C(2); `needs_cdc`-bearing
= D(14); 114 + 14 = **128**, no overlap. So the apex itself encodes *both* axes:
shape-D is MINIMAL-5-with-`needs_cdc` (the in-domain FIS telemetry), shapes A/B/C are
the rollup-summary form. `[HIGH · OBSERVED]`

---

## 2. Edge / level / CDC — the per-table polarity, by family `[HIGH · OBSERVED]`

`edge_triggered` and `needs_cdc` are present in every family, but their *distribution*
is a per-table fingerprint. Re-run this session (`yq '[.[]|select(.edge_triggered==…)]|length'`):

| Table | family | edge | level | CDC `true` | the level-set character |
|-------|--------|-----:|------:|-----------:|-------------------------|
| SDMA | MINIMAL-5 | 223 | 31 | **0** | the ELA/ERG/DRE/CCE compute-fault block (sticky RAS) |
| io-fabric | MINIMAL-5 | 226 | 17 | **0** | AXI-write integrity + `*_timeout` watchdogs + apbblk CAM-hit |
| PCIe | RICH-9 | 216 | 12 | 95 | 4 Core-ELA + 6 `fis_sprot` + 2 `nts_iso` timeouts |
| **HBM** | RICH-9 | **31** | **192** | 196 | **INVERTED** — all 192 `hbm_ctrl_interrupt_gen[ch][cause]` sticky status |
| TPB | TPB-9 | 158 | 58 | 130 | ECC/parity + NX-IRQ + per-engine FP-status |
| D2D | MINIMAL-5 | 67 | 149 | **0** | ERG + all 148 controller/PHY status |
| CC | CC-8 | 92 | 6 | 31 | exactly the 2 ECC + 4 FP-status (sticky state) |
| TOP_SP | TOPSP-7 | 74 | 8 | **0** | `err_sem_overflow` + 2 ERG-ECC + 5 NX-core |
| APEX | APEX-10 | 24 | 104 | (14 carry `needs_cdc`) | 104 summary re-latches are level; 24 pulse-criticals are edge |

> **GOTCHA — the HBM edge/level inversion is the lone polarity outlier (H-ATLAS-2).**
> Every other leaf is edge-majority; **HBM is 192 level / 31 edge** because the 192
> `hbm_ctrl_interrupt_gen` entries are persistent sticky DDR-controller status bits
> (ECC / temperature / parity), not one-shot pulses. A reimplementation that
> hard-codes "controller interrupts are edge" (true for PCIe's `intc_triggers_core_out`)
> mis-programs `int_posedge_grp` for every HBM channel cause. The trigger table is the
> source-of-truth for `int_posedge_grp` per bit, per domain. `[HIGH · OBSERVED]`

> **NOTE — SDMA / io-fabric / D2D / TOP_SP declare ZERO `needs_cdc==true`.** All four
> MINIMAL-5/TOPSP-7 tables do their clock-domain crossing **inside the source block**
> before the trigger bus (SDMA), or at the controller/MPCS (D2D, per its verbatim
> comments). The INTC still carries a per-trigger CDC edge-gen stage gated by
> `int_cdc_bypass_grp` whose `no_msix` variant notes "the CDC syncro is still in the
> path regardless" ([INTC 4-group](../csr/intc-4group.md)) — so a leaf can declare no
> crossing while the INTC unconditionally resynchronises. **Gate CDC on the
> `needs_cdc` *value*, not its presence.** `[HIGH · OBSERVED]`

**The `source_clock` ⇔ `needs_cdc==true` co-population invariant.** In every family
that carries `source_clock` (RICH-9, TPB-9, CC-8) the key set populates *exactly* the
`needs_cdc==true` rows (CC: both = 31; verified), and names the INTC-side **destination**
clock the CDC sampler reads into: HBM → `dfi_hdr_clk_occ_out`, PCIe → `intc_core_clk`,
TPB → `clk_core_gated` (×125) + `clk_mem_top` (×1, the lone `ham_intr`), CC → `clk_1p2`
(the 1.2 V compute/Q7 domain). The MINIMAL-5 tables omit `source_clock` *because* they
declare no CDC. `[HIGH · OBSERVED]`

---

## 3. The schema-parse / normalize algorithm `[structure HIGH · OBSERVED; values OBSERVED]`

The one algorithm a control plane needs: ingest any of the eleven tables, detect its
family, and normalize each entry into a uniform internal record. The family detector
is a 3-key decision tree (the §1.1 discriminators); the per-bit INTC programming
(`int_posedge_grp` from `edge_triggered`, CDC from `needs_cdc`/`source_clock`) follows
from the normalized record.

```c
/* Trigger-YAML schema atlas — family detection + normalization.
 * The key-presence discriminators are byte-OBSERVED from the eleven Cayman YAMLs;
 * the per-bit INTC programming maps to int_posedge_grp / int_cdc_bypass_grp
 * (see intc-4group.md). YAML array order == INTC trigger-bit order (universal). */

typedef enum {
    FAM_MINIMAL5,   /* SDMA, io-fabric, D2D, TOP_SP-baseline */
    FAM_RICH9,      /* PCIe, HBM        — nmi_mask on every entry        */
    FAM_TPB9,       /* TPB              — msix_mask + tog2pul_only        */
    FAM_CC8,        /* CC               — msix_mask, no tog2pul_only      */
    FAM_TOPSP7,     /* TOP_SP           — minimal-5 + 2 nmi_mask ECC rows */
    FAM_APEX10,     /* peb_intc         — critical present (4 sub-shapes) */
} trig_family_t;

/* One normalized trigger record (the superset; absent keys -> sentinels). */
typedef struct {
    const char *trigger, *name, *description;   /* always present                 */
    bool  edge_triggered;                       /* always present -> int_posedge   */
    int   needs_cdc;                             /* -1 = key absent                 */
    const char *source_clock, *source_reset_n;  /* NULL if absent                  */
    int   nmi_mask, nmi_msix_mask;              /* -1 = absent (no NMI rollup)     */
    int   msix_mask;                            /* -1 = absent                     */
    bool  tog2pul_only;                         /* false unless TPB edge-detect    */
    int   critical;                             /* -1 = absent (apex only)         */
    const char *source_path, *source_block;     /* NULL except apex shape-C        */
} trig_rec_t;

/* Family detection: 3-key decision tree over the per-table key union.
 * `u` is the set of keys observed across the whole table (yq '[.[]|keys[]]|unique'). */
static trig_family_t detect_family(const keyset_t *u) {
    if (has(u, "critical"))       return FAM_APEX10;     /* apex is the only critical-bearer  */
    if (has(u, "tog2pul_only"))   return FAM_TPB9;       /* hard TPB fingerprint              */
    if (has(u, "msix_mask"))      return FAM_CC8;        /* msix_mask w/o tog2pul -> CC        */
    if (has(u, "source_clock"))   return FAM_RICH9;      /* nmi_mask + source_* -> PCIe/HBM    */
    if (has(u, "nmi_mask"))       return FAM_TOPSP7;     /* nmi_mask, no source_* -> TOP_SP    */
    return FAM_MINIMAL5;                                 /* SDMA/io-fabric/D2D                 */
}

/* Normalize one YAML entry. Missing keys collapse to sentinels so downstream code
 * is family-agnostic. The bit index is the entry's array position (NOT a key). */
static void normalize_entry(const yaml_map_t *e, unsigned bit_idx, trig_rec_t *r) {
    r->trigger        = yaml_str(e, "trigger");           /* keeps [N]/[N][M] brackets */
    r->name           = yaml_str(e, "name");
    r->description     = yaml_str_or_null(e, "description"); /* may be null/placeholder */
    r->edge_triggered = yaml_bool(e, "edge_triggered");
    r->needs_cdc      = yaml_int_or(e, "needs_cdc", -1);    /* -1 = absent (PCIe/D2D partial) */
    r->source_clock   = yaml_str_or_null(e, "source_clock");
    r->source_reset_n = yaml_str_or_null(e, "source_reset_n");
    r->nmi_mask       = yaml_int_or(e, "nmi_mask", -1);
    r->nmi_msix_mask  = yaml_int_or(e, "nmi_msix_mask", -1);
    r->msix_mask      = yaml_int_or(e, "msix_mask", -1);
    r->tog2pul_only   = yaml_bool_or(e, "tog2pul_only", false);
    r->critical       = yaml_int_or(e, "critical", -1);
    r->source_path    = yaml_str_or_null(e, "source_path");
    r->source_block   = yaml_str_or_null(e, "source_block");
    /* INVARIANT: array index IS the INTC trigger-bit position; group = idx>>5, bit = idx&31. */
    (void)bit_idx;
}

/* Program one 4-group INTC bit from a normalized record (the leaf-side action). */
static void program_intc_bit(IntcUnit *u, unsigned idx, const trig_rec_t *r) {
    unsigned g = idx >> 5, b = idx & 31;                  /* 4 groups x 32 bits */
    /* int_posedge_grp realizes edge_triggered: 1 = posedge/edge, 0 = level. */
    intc_rmw_bit(&u->grp[g].int_posedge, b, r->edge_triggered);
    /* CDC: gate on the VALUE, not presence. needs_cdc<=0 => bypass the per-trigger sync. */
    bool cdc = (r->needs_cdc > 0);                        /* -1 (absent) and 0 both => bypass */
    intc_rmw_bit(&u->grp[g].int_cdc_bypass, b, !cdc);     /* tog2pul_only takes the edge path */
    if (r->tog2pul_only) intc_set_tog2pul(u, idx);        /* TPB PE-array FP-summary only */
}
```

> **NOTE — array index is the bit, never a key.** No table encodes the INTC bit index
> as a field; the index **is** the entry's array position, and `group = idx>>5`,
> `bit = idx&31`. This is proven for D2D by its verbatim late-tail comment ("added late
> in Cayman so adding at end … to prevent existing interrupt indexes changing";
> [D2D §5](pcie-hbm-tpb-d2d-triggers.md)) and corroborated for TPB by the 256-bit
> `intc_bypass` map. A parser that re-orders entries (e.g. by sorting on `name`)
> destroys the bit mapping. `[HIGH · OBSERVED]`

---

## 4. The shared sub-vectors — the FIS shim family across all tables `[HIGH · OBSERVED]`

Cutting *across* the six families is one recurring sub-vocabulary: the **FIS shim**
(every `FIS_0` container contributes three interrupt vectors), the **NOTIFIC** queue
mirror, and the **`intc_top_retrigger`** mailbox. These reappear, byte-identical in
description, in many tables — they are the schema atlas's "shared structs". The
canonical widths (the **routing-leaf counts**, head-anchored to dodge the count
hazard; [errtrig §3.1](errtrig-fis-routing.md)):

| shared sub-vector | width | shape | which tables carry it | section |
|-------------------|------:|-------|-----------------------|---------|
| `intc_top_retrigger[0..127]` | **128** | edge, MINIMAL-5 | io-fabric only | [IOF §3 A](io-fabric-triggers.md) |
| `fis_errtrig_intr[0..49]` | **50** | edge | PCIe, TPB, D2D, CC, TOP_SP, io-fabric (and the SDMA `fis_errtrig` mirror) | [errtrig §2.3](errtrig-fis-routing.md) |
| `notific` (one chain of the 50) | **25** | edge | the per-chain user *or* amzn NOTIFIC half | [IOF §3 B/H](io-fabric-triggers.md) |
| `fis_sprot_intr` | **12** (= 6 × 2 sub-blocks) | edge | io-fabric, CC, D2D (2 sprot inst); TOP_SP carries **6** (1 inst); TPB **30** (5 inst) | [errtrig §2.2](errtrig-fis-routing.md) |
| `fis_cntrl_intr[0..4]` | **5** | edge | PCIe, HBM (`_hbm_top_stg`), TPB, D2D, CC, TOP_SP, io-fabric | [errtrig §2.1](errtrig-fis-routing.md) |

> **GOTCHA — the `fis_errtrig_intr` count hazard (H-ATLAS-3).** A naive
> `rg -c 'fis_errtrig_intr'` returns **100**, not 50 — each YAML entry carries *both*
> a `trigger:` and a `name:` line. Anchor on the entry head:
> `rg -c '^- trigger: fis_errtrig_intr\['` = **50**. The same doubling inflates every
> bare `rg -c` over any `*_triggers.yaml`; always head-anchor on `^- trigger:` or use
> `yq 'length'`. This is why every count on this page is `yq`-derived, never a bare
> grep. `[HIGH · OBSERVED]`

> **QUIRK — `fis_sprot_intr` is the same 6-cause vector at three different
> multiplicities.** The *cause* set is invariant (remapper-deny / R>AR delta / tmu-tout
> / B>AW delta / qos-PMU / spare), but the per-table **instance multiplier** varies:
> TOP_SP = 1 inst (6 entries), io-fabric/CC/D2D = 2 inst (12), TPB = 5 inst (30). A
> reimplementer sizing the sprot vector must read the multiplicity per table, not
> assume 12. The canonical 6-entry single-index form (with the meaningful descriptions)
> lives in `top_sp_triggers.yaml`; `pcie_triggers.yaml` carries generic placeholders
> that are **level**-triggered. `[HIGH · OBSERVED]`

---

## 5. The SOURCE MAP — source family → schema → routing leaf → apex → delivery `[HIGH · OBSERVED]`

The second half of the synthesis: every trigger source threaded end-to-end. The
**universal routing primitive** is the **errtrig PAIR** — two `intc_4grp` units
(`TRIG_0` + `TRIG_1` = **256-source capacity**) + one `notific_1_queue`, in a `0x3000`
container; the flavor tracks **privilege, not domain** (USER → `intc_4grp_msix`,
host-reachable; AMZN → `intc_4grp_no_msix`, emits 4 severity wire-ORs + `Mask_msi_x`
= one `nmi_out`). 1,932 physical INTC instances; **962 generator pairs** (428 USER +
534 AMZN); the apex is a **flavor pair** (`PEB_INTC_TRIG_0` no_msix + `PEB_INTC_MSIX`),
no `TRIG_1`. `[HIGH · CARRIED from physical-intc-instances.md / errtrig-fis-routing.md]`

### 5.1 The master source map `[HIGH · OBSERVED counts; routing CARRIED]`

Each leaf domain → its schema family → the **errtrig PAIR** it fills → its apex
roll-up wire → delivery. Every leaf table ≤ 256 fits one pair; CC(98) and TOP_SP(82)
fit a single 4-group logically but still instantiate a physical pair (the generator
always emits two `intc_4grp`; [physical §3 KEY FINDING](physical-intc-instances.md)).

| Leaf domain | leaf count | schema | fills | apex roll-up wire(s) | apex idx | delivery |
|-------------|-----------:|--------|-------|----------------------|----------|----------|
| **SDMA** | 254 | MINIMAL-5 | errtrig PAIR (≤256) | `se{0,1}_sdma_nmi[0..31]` (64) + `sdma_{d2h,h2d}_nmi` (2) | 7–38, 42–73, 75–76 | summary |
| **io-fabric** | 243 | MINIMAL-5 | `io_intc_rdm` 1grp + errtrig | `io_fabric_apbblk_list_intr_local` (crit) + `se{0,1}_apbblk_list_*` (shape-D) | 108 + (apbblk) | summary + 4 crit/shape-D |
| **PCIe** | 228 | RICH-9 | errtrig PAIR | `pcie_m0_nmi` (shape-C) + `pcie_a0_nmi` (shape-C) + `pcie_se{0,1}_b_combined_nmi` | 0, 1, 5, 6 | summary (2 named) |
| **HBM** | 223 | RICH-9 | errtrig PAIR | `hbm_{0,1}_nmi` + `hbm_inttrig_{cattrip,temp_change}_r[0,1]` (**4 crit**) | 74 + 80–83 | summary + **4 critical** |
| **TPB** | 216 | TPB-9 | errtrig PAIR | `se{0,1}_tpb_nmi[0,1]` (4) — **no critical** | (per-tile) | summary only |
| **D2D** | 216 | MINIMAL-5 | errtrig PAIR | `d2d_combined_nmi` (D2D[0-7], 8 links) — **no critical** | (combined) | summary only |
| **CC (PREPROC)** | 98 | CC-8 | errtrig PAIR | `cc_top_{0,1}_nmi` — **no critical** | 112, 113 | summary only |
| **TOP_SP** | 82 | TOPSP-7 | errtrig PAIR | `top_sp_combined_nmi` (TopSP[0-9], 10) — **no critical** | 77 | summary only |
| *(PEB-local criticals)* | — | APEX shape-B | direct to apex | PVT(7) / SPI(8) / axi2apb(4) / ERG(2) / NSM(1) / GPIO+I2C+misc_ram(3) / APB-flush(2) | scattered | **32 critical** |
| *(PEB FIS telemetry)* | — | APEX shape-D | direct to apex | `fis_sprot_intr[0..4]` (5) + `fis_cntrl_intr[0..4]` (5) + `se*_apbblk_list_*` (4) | 118–127 | un-NMI-masked |

Apex roll-up: **96 summary `nmi_out` wires + 32 direct PEB-local criticals = 128**
([apex §1](peb-cc-topsp-triggers.md)). SDMA dominates at **64/128** (half the apex).
`[HIGH · OBSERVED]`

### 5.2 The `edge_triggered` → `int_posedge_grp` binding `[HIGH · OBSERVED leaf; CARRIED INTC]`

The `edge_triggered` bit binds **directly** to the INTC `int_posedge_grp` register
(per-bit: `1` ⇒ posedge/edge, `0` ⇒ level; [INTC §6.4](../csr/intc-4group.md)). Each
table is the source-of-truth for how firmware programs `int_posedge_grp` for that
domain's bit range — hence the §2 HBM-inversion warning is a *programming* hazard, not
just a documentation note. `[HIGH/OBSERVED leaf; CARRIED INTC]`

### 5.3 The critical fast-path — which sources earn a dedicated apex bit `[HIGH · OBSERVED]`

Exactly **32 of 128** apex inputs carry `critical:1` (re-verified
`yq '[.[]|select(.critical==1)]|length'` = 32). The critical flag is the
SoC-survival fast-path — firmware reacts **without decoding a per-domain summary**.
The 32 (sum verified = 32): HBM thermal (4) + PVT (7) + SPI slave (8) + axi2apb (4) +
NSM AXI timeout (1, idx 111) + ERG uncorrectable (2) + io_fabric apbblk (1) + APB
flush (2) + GPIO/I2C/misc_ram (3).

> **The HBM critical fast-path is the only domain-leaf rollup that gets a critical
> bit.** `hbm_inttrig_{cattrip,temp_change}_r[0,1]` **bypass** the generic
> `hbm_{0,1}_nmi` summary so firmware reacts to a catastrophic-temperature trip (the
> HBM M=5 cause) before the device cooks. **TPB, D2D, CC, TOP_SP, SDMA-per-engine, RDM
> carry NO critical** — even an uncorrectable Q7-mem ECC (`cc_seq_mem_uncerr`) or a
> D2D link-down rides the generic per-tile/combined summary, and firmware must decode
> the leaf errtrig. A CATTRIP gets a hardware fast-path; a D2D link-down does not — a
> real, structural difference, not a documentation artifact. `[HIGH · OBSERVED]`

> **GOTCHA — NSM reaches the core by TWO paths (the SOURCE-MAP keystone).** The NSM
> AXI-timeout is the one isolation-SM source with a *direct* critical apex bit:
> (a) *summarized* — NSM → `isolation_sm_nsm_axi_timeout_detected`
> (`reset_handshake_intr[11]`, a PCIe-**leaf** source) → `isolation_mode_enter[14]` →
> PCIe leaf summary → `pcie_*_nmi` apex bit; and (b) *direct fast-path* —
> `intr_peb_nsm_axi_timeout` at apex **idx 111** (`critical:1`, LEVEL,
> `nmi_mask:0`), which matches the firmware cause enum (`intc_peb_intc_enums.h`
> NSM = 111). All *other* isolation-SM sources (`reset_handshake_intr[8..15]`) live one
> level down in `pcie_triggers.yaml`, summarized only. NSM is the exception.
> `[HIGH · OBSERVED; full chain in nsm-flow-unified.md]`

---

## 6. The apex bit-reversal + the verbatim-typo ledger `[HIGH · OBSERVED]`

Two cross-table hazards a string-matching consumer must internalize.

### 6.1 The SDMA apex bit-reversal `[HIGH · OBSERVED]`

> **CORRECTION / HAZARD — apex bit index ≠ SDMA queue number.** The apex SDMA trigger
> **index counts UP** while the NAME's SDMA queue number **counts DOWN**, verified
> against `peb_intc_triggers.yaml`:
> `se0_sdma_nmi[0]` → name `se0_sdma_31_summary` (queue 31);
> `se0_sdma_nmi[31]` → name `se0_sdma_0_summary` (queue 0). Identical reversal on SE1.
> So apex bit-position `i` maps to **physical queue `[31 − i_local]`**. A tool reading
> the apex bit index as the queue number gets the complement. **Trust the NAME string
> `se{e}_sdma_{q}_summary`, never the bit index.** `[HIGH · OBSERVED]`

### 6.2 The verbatim-typo ledger (do NOT "fix" when string-matching) `[HIGH · OBSERVED]`

The RTL-generated tables preserve source typos byte-exact. A consumer matching the
binary symbol table must reproduce them:

| typo (verbatim) | correct | where | count |
|-----------------|---------|-------|------:|
| `isolatio_sm_nsm_axi_timeout_detected` | `isolation_sm…` | PCIe `reset_handshake_intr[11]` | 1 |
| `interrrupts` / `interrrupt` (triple-r) | `interrupts` | `fis_sprot_intr[4]` (sdma/io-fabric) | 3 |
| `wr_bufffer` (triple-f) | `wr_buffer` | io-fabric `intc_notific_intr[9..16]` | 8 |
| `doesn not exist` | `does not exist` | SDMA `access_missing_app_engine` | 1 |
| `# All available FIS_ERRRIG triggers` (triple-r) | `…ERRTRIG…` | PCIe / D2D section comments | 2 |
| `core_reset_dassertion` / `surpise_down_error` / `corrected_interna_eerror` / `breap_queue_error` | (various) | D2D `name` fields (both ctrls) | 8 |
| NX-IRQ "SP NX Interrupt 0..4" copy-paste | (per-engine) | TPB `dve_/act_/pool_/pe_nx_interrupt_*` descriptions | 20 |

> **GOTCHA — name vs description authority flips per table.** In TPB the `name` field
> is authoritative and 20 NX-IRQ `description`s are wrong (copy-pasted); in D2D the
> `trigger` *path* is correctly spelled while 8 `name` fields carry typos — so the
> **path** is the reliable D2D identifier and the **name** the reliable TPB one. There
> is no single "trust X" rule across tables; the atlas's per-table note is required.
> `[HIGH · OBSERVED]`

---

## 7. The end-to-end cascade `[chain HIGH · OBSERVED; final hop INFERRED]`

The consolidated picture: any source, through its schema-typed leaf, to the Q7.

```text
 L0  SOURCE EVENT  (parsed against its FAMILY schema; §1/§3)
       MINIMAL-5 (SDMA/io-fab/D2D/TOP_SP) | RICH-9 (PCIe/HBM) | TPB-9 | CC-8
       array index = INTC bit; edge_triggered -> int_posedge_grp; needs_cdc -> CDC
         |
         v
 L1  LEAF ERRTRIG intc PAIR  (the universal primitive; 256-cap; flavor = privilege)
       USER  -> intc_4grp_msix    --MSI-X--> PCIe host (direct, host-reachable)
       AMZN  -> intc_4grp_no_msix -> 4 severity wire-ORs (Error/Abort/Fatal/Log)
                                     + Mask_msi_x summary = one nmi_out
         |  (Abort wire-OR -> Sunda @0x300 scan_dump/clock_stop/sram_wp -> abort page)
         v
 L2  PEB APEX  peb_intc_triggers.yaml  (APEX-10, 4 sub-shapes A/B/C/D)
       128 inputs = 96 nmi_out summaries (shape A/C) + 32 direct criticals (shape B)
                    + 14 shape-D FIS/apbblk telemetry  (file order = bit order)
       {PEB_INTC_TRIG_0 no_msix + PEB_INTC_MSIX twin}, per-PEB  (flavor pair, no TRIG_1)
         |  MSI-X
         v
 L3  IOFIC / io_fabric cascade  ->  Q7 / "Pacific" management core  and/or a GIC   [INFERRED]
       .critical=true  -> fast-path handler WITHOUT decoding a summary  (the 32)
       .critical=false -> descend into the named leaf summary register   (the 96)
       (apex-pending-bit -> Q7/GIC vector map = firmware/HW-owned, in no shipped artifact)
```

The leaf → apex chain is `[HIGH · OBSERVED]` (the schema, the bit order, the flavor
split, the 96+32 apex composition all read from bytes). The final **apex → Q7/GIC
vector hop is INFERRED** — a GIC is confirmed to exist (the CXELA500 ELA "connects to a
GIC as an IRQ input"), but the pending-bit → vector map lives in firmware/HW, not the
YAML or intc JSON. This page does **not** assert the final hop as fact. `[INFERRED]`

---

## 8. Cross-generation — the schema families across gens `[Cayman HIGH · OBSERVED; v5 header-OBSERVED]`

The **six families are a Cayman/v3 fact**. The cross-gen `arch-headers` (Sunda,
Maverick) reshape the tails but keep the family backbone:

| change | Sunda | Maverick (v5) | net |
|--------|-------|---------------|-----|
| `fis_cntrl_intr[0..4]` (5) | folded into a flat `sprot_intr[0..54]` | **collapsed to scalar** `fis_cntrl_intr` | −4 / −4 |
| FIS family | replaced by one flat `sprot_intr` bus | adds `parity_{addr,data}_error_interrupt` | varies |
| apex (`peb_intc`) | 97 (35 crit), finer per-instance tap | **119 (79 crit)**, `functional_test_required` key, `*_vec_q` arrays | +`critical` widen |
| D2D | **no file** (package-level) | **7-entry abstract** `d2d_intr_{fatal,error,info}` black-box | model differs |
| per-IP INTC layer | — | **13 new per-block INTC schemas** (decentralized) | Cayman has zero |

> **NOTE — v5 is header-OBSERVED only; flag v5-interior INFERRED.** The Maverick
> header / entry-count / key-set are OBSERVED (the schema banner, `yq 'length'`), but
> the v5 *interior* — the runtime semantics behind the collapsed `fis_cntrl`, the
> `*_vec_q` arrays, the 7-entry abstract D2D, the per-IP INTC fleet — is **INFERRED**.
> Maverick adds a `functional_test_required` per-source key (a 7th family discriminator
> not present in any Cayman table) and replaces named summaries with generic
> `*_vec_q[N]` vector arrays — a *structurally different* schema generation, not a tail
> diff. Do not read v5 facts as Cayman facts; the six-family taxonomy is the v3 anchor.
> `[header HIGH · OBSERVED; v5-interior INFERRED]`

---

## 9. Reimplementer's checklist `[HIGH · OBSERVED unless noted]`

1. **Detect the family before parsing** (3-key decision tree, §3): `critical` ⇒ APEX;
   `tog2pul_only` ⇒ TPB; `msix_mask` (no `tog2pul`) ⇒ CC; `source_clock` ⇒ RICH-9;
   `nmi_mask`-only ⇒ TOP_SP; else MINIMAL-5. `nmi_mask` and `msix_mask` are mutually
   exclusive. `[HIGH · OBSERVED]`
2. **Array index IS the INTC bit** (`group = idx>>5`, `bit = idx&31`); never re-order
   entries. `[HIGH · OBSERVED]`
3. **Program `int_posedge_grp` from `edge_triggered` per bit** — and remember the
   **HBM inversion** (192 level, 31 edge). `[HIGH · OBSERVED]`
4. **Gate CDC on `needs_cdc` VALUE, not presence**; sample the `needs_cdc==true` rows
   into their `source_clock` (HBM→`dfi_hdr_clk_occ_out`, PCIe→`intc_core_clk`,
   TPB→`clk_core_gated`, CC→`clk_1p2`); MINIMAL-5 tables do CDC internally. `[HIGH · OBSERVED]`
5. **One errtrig PAIR (256-cap) per domain, flavor by privilege** (USER→msix,
   AMZN→no_msix); every domain — even CC(98)/TOP_SP(82) — gets a physical pair.
   `[HIGH · CARRIED]`
6. **Wire 96 summary `nmi_out` + 32 criticals + 14 shape-D = 128 apex inputs**; hook
   the 32 criticals (HBM-thermal/NSM/PVT/SPI/ERG/…) as direct fast-paths; TPB/D2D/CC/
   TOP_SP have none. `[HIGH · OBSERVED]`
7. **Trust NAME for SDMA apex queues** (bit-reversal `31−i`); reproduce the §6.2 typos
   byte-exact when string-matching. `[HIGH · OBSERVED]`
8. **The apex → Q7/GIC final hop is `[INFERRED]`** — a GIC exists, the vector map is
   firmware-owned. For a v5 target, add the Maverick layers (per-IP INTC,
   `functional_test_required`, `*_vec_q` arrays, 119-entry apex) — header-OBSERVED,
   interior INFERRED. `[INFERRED]`

---

## 10. Hazard / anomaly ledger

| # | Hazard | Grade |
|---|--------|-------|
| H-ATLAS-1 | **`nmi_mask` ⊕ `msix_mask`**: no entry carries both — the cleanest single-bit family discriminator (NMI-rollup vs MSI-X). | `[HIGH · OBSERVED]` |
| H-ATLAS-2 | **HBM edge/level inversion**: 192 level / 31 edge — the lone polarity outlier; hard-coding "controller IRQs are edge" mis-programs every HBM channel. | `[HIGH · OBSERVED]` |
| H-ATLAS-3 | **`fis_errtrig` count hazard**: bare `rg -c 'fis_errtrig_intr'` = 100 (trigger+name lines), not 50; head-anchor `^- trigger:`. | `[HIGH · OBSERVED]` |
| H-ATLAS-4 | **`fis_sprot` multiplicity varies**: 6-cause vector × {1,2,5} instances → TOP_SP 6 / io-fab·CC·D2D 12 / TPB 30. Don't assume 12. | `[HIGH · OBSERVED]` |
| H-ATLAS-5 | **TPB-9 ≠ CC-8 by one key**: `tog2pul_only` is the TPB-only fingerprint; same lineage (CC = TPB POOL IP), different schema. | `[HIGH · OBSERVED]` |
| H-ATLAS-6 | **SDMA apex bit-reversal**: `se{e}_sdma_nmi[i]` → physical queue `31−i`. Trust the NAME, not the index. | `[HIGH · OBSERVED]` |
| H-ATLAS-7 | **`needs_cdc` partial in PCIe(212)/D2D(67) only**: absence ≠ parse error — it means "no per-trigger CDC metadata". | `[HIGH · OBSERVED]` |
| H-ATLAS-8 | **Verbatim typos preserved** (§6.2): 7 distinct typo families; name-vs-path authority flips per table. | `[HIGH · OBSERVED]` |
| H-ATLAS-9 | **APEX-10 spans 4 sub-shapes** (80/32/2/14): `source_path` on only 2, `nmi_mask` on only 114 — not all 128. | `[HIGH · OBSERVED]` |
| H-ATLAS-10 | **NSM 2-path delivery**: idx-111 direct critical + PCIe-leaf summary — the one isolation-SM source with a dedicated apex bit. | `[HIGH · OBSERVED]` |

---

## See also

* [SDMA triggers](sdma-triggers.md) — the 254-source MINIMAL-5 leaf (the family anchor).
* [IO-fabric triggers](io-fabric-triggers.md) — the 243-source IO-die aggregator + `intc_top_retrigger[128]`.
* [PCIe / HBM / TPB / D2D triggers](pcie-hbm-tpb-d2d-triggers.md) — the RICH-9 / TPB-9 / MINIMAL-5 four-domain shapes.
* [PEB apex / CC / TOP_SP](peb-cc-topsp-triggers.md) — the APEX-10 four-sub-shape rollup + CC-8 + TOPSP-7.
* [Physical INTC instances](physical-intc-instances.md) — the 1,932-instance census + errtrig-pair primitive.
* [errtrig / FIS routing](errtrig-fis-routing.md) — the L0→L3 cascade + 962 pairs + the FIS shim source family.
* [NSM flow (unified)](nsm-flow-unified.md) — the NSM fault → isolation → IRQ chain (the security synthesis).
* [CSR — INTC 4-group](../csr/intc-4group.md) — `int_posedge_grp` / `int_cdc_bypass_grp` / the errtrig pair.
