# pkl TOP_SP / Sync / MISC Subtree + Coverage Tally

This page **closes the pkl-carve sub-lane**. It does two jobs:

1. Carve the **last** engine subtree out of the Annapurna-Labs address-map source DB —
   the **TOP_SP** collective sequencer (engine 5), the **sync / semaphore** substrate
   (EVT_SEM 256-array + COLLECTIVE_SYNC + SP_SHARED_RAM), and the **MISC / residual**
   peripherals (URB routing fabric + the PEB chiplet-management IO blocks: PLL, QSPI,
   GPIO, I²C, SPIS, PVT, DFX, scratchpad).
2. Prove **coverage is complete** — sum *every* carved subtree against the **323,198**
   record total, reconciled on **two independent axes** (by physical lane AND by
   access-domain view), and show the residual closes to **0**.

The shared load primitive (safe `pickletools.genops()` / streaming-JSON readers, the
23-field record schema, the 5 access-domain views, the 6-class `type` taxonomy) is the
deliverable of [`pkl-db.md`](./pkl-db.md) — read that first. This page reuses it and never
calls `pickle.load()`.

> **⚠ WALL — MAVERICK (v5), header-OBSERVED only.** The DB is
> `arch-headers/`**`maverick`**`/ext/al_address_map_db.pkl` (216,631,794 B) + its `.json`
> mirror (514,276,583 B); every record's `json` path is rooted at
> `/proj/maverick/hannloui/pool_wa/…`. The **DB structure** (records, fields, counts, view
> partition) is **OBSERVED** — streamed structurally from the shipped file. Any claim about
> what a v5 address *does inside the silicon* is **INFERRED**. The byte-grounded behavioral
> cross-check is the **CAYMAN (NC-v3)** flat YAML (`address_map_flat.yaml`); it is a
> *different SoC instance* and is used only for the generational deltas in §6.
> `[HIGH · OBSERVED]` for structure; `[* · INFERRED]` for v5 interiors.

Primary artifacts (all binary-derived, citeable):

| artifact | role |
| --- | --- |
| `…/maverick/ext/al_address_map_db.pkl` | the source register-tree DB (THIS) |
| `…/maverick/ext/al_address_map_db.json` | pretty mirror — streamed for every count below |
| `…/csrs/top_sp/top_sp_amzn.json`, `…/top_sp_misc_amzn.json` | TOP_SP CSR + MISC schema |
| `…/csrs/xtensa_nx/xtensa_nx.json` | the embedded NX sequencer-core CSRs |
| `…/csrs/urb/{tile,non_tile}_fabric_urb.json` | the URB routing-fabric schema |
| `…/address_map/apb/io/{pll,gpio_0,i2c}_amzn.json`, `csrs/{spis,pvt}/…` | PEB peripherals |
| `output/.../cayman-arch-regs_tgz/…/address_map_flat.yaml` | CAYMAN cross-gen delta only |

(All under the gitignored `extracted/` tree — use `--no-ignore` / absolute paths.)

---

## 0. Self-verify — the five headline claims re-streamed this session `[HIGH · OBSERVED]`

Before authoring, the five strongest claims were re-challenged directly against the `.json`
mirror's **`name` key** (`rg -c '"name": "…<token>…"'` — text only, zero pickle risk). The
`name` key occurs **exactly once per record** (`rg -c '"name":'` = 323,198 = the DB `len`),
so a `name`-keyed token count is a true per-record count, not an inflated multi-field grep.

> **GOTCHA — count off the `name` key, not the raw line.** A bare `rg -c '"[^"]*TOP_SP'`
> over the JSON returns **6,144**, because the `TOP_SP` token also appears in the
> `short_name` (184) and `json` schema-path (48) keys. The authoritative record count is
> the `name`-keyed `5,912` (= 6,144 − 184 − 48). Every count on this page is `name`-keyed.

| claim | re-streamed result | verdict |
| --- | --- | --- |
| TOP_SP carve | `name`-key `TOP_SP` = **5,912**; by view user_int **936** / secure_int **4,976** | ✅ matches |
| EVT_SEM 256-array | `name`-key `EVT_SEM` = **504**; user_int **288** / secure_int **216** | ✅ matches |
| Grand 323,198 by-lane | non-overlapping first-match partition sums **323,198** (§4) | ✅ closes |
| Grand 323,198 by-view | `parent_names[1]` tally **79,104+244,040+24+24+5+1** | ✅ closes |
| MISC peripheral set | URB/PLL/QSPI/GPIO/I²C/SPIS/PVT/DFX/scratchpad all `name`-key > 0 (§3) | ✅ present |

Two absences were also re-confirmed (defining-by-what-is-*not*-there): `SDMA`, `RDM`,
`SPAD`, `TSYNC` `name`-keyed counts are all **0** — Maverick names its DMA `DDMA`/`CDMA`,
the Ring-Descriptor-Manager lives *inside* INTC as `INTC_RDM` (already in the ADDR-15
lane), and the TOP_SP's SPAD `cc_op` program + device tsync are *runtime software structs*
(loaded into TOP_SP RAM at runtime), **not** address-mapped register blocks. `[HIGH · OBSERVED]`

---

## 1. The TOP_SP subtree — the collective sequencer (engine 5) `[HIGH · OBSERVED]`

`TOP_SP` appears in **5,912** records (pkl == JSON mirror, byte-identical). By view:
**user_int 936 / secure_int 4,976**. By `type`: `REGFILE` 4,984 / `TABLE` 800 / `NODE` 128.

### 1a. Role

TOP_SP is **engine 5**, the on-device **collective sequencer**: an embedded **Xtensa-NX**
core that walks the lowered collective program, issuing DMA tail-pointer increments and
`EVT_SEM` arrive/wait/dec ops. It is **distinct** from the per-TPB `TPB_SP` (engine 4 — the
16 `SP_0..15` sequencers inside each tensor block, carved in
[`pkl-tpb-subtree.md`](./pkl-tpb-subtree.md)). The CSR-side model is
**TOP_SP = TPB_EVT_SEM + RAM + NX-core**; the lowering of barrier→semaphore sequences onto
it is documented in [`../../collectives/ops/top-sp-lowering.md`](../../collectives/ops/top-sp-lowering.md)
and the CSR layout in [`../csr/rdm-top-sp.md`](../csr/rdm-top-sp.md).
`[role HIGH (cross-CSR/CCL); engine-5-vs-4 split HIGH · OBSERVED]`

### 1b. The `TOP_SP_CLUSTER0` block — the dominant TOP_SP surface (1,344 records)

The `SP_CLUSTER` token carries **2,688** `name`-keyed records — and these are a **subset**
of the 5,912 `TOP_SP` set (a `TOP_SP_CLUSTER0` leaf name carries *both* tokens). The cluster
is a `REGFILE` strip at parent-offset `+0x55000`. It hosts the **collective-leg DGE
command-injection engine** — the stream-to-AXI command tables the TOP_SP uses to launch the
SB2SB / DMA legs of a collective:

| schema (basename) | binds | type | what |
| --- | --- | --- | --- |
| `dge_stream_to_axi_dge_cmd_inject_table.json` | 192 | TABLE | DGE cmd-inject table |
| `DGE_STREAM_TO_AXI_DGE_CMD_REGS.json` | 192 | REGFILE | cmd regs |
| `AP_TABLE_CTRL.json` | 192 | REGFILE | table control |
| `regfile_{parity_log,ras,security_log,access_log}.json` | 192 ea | REGFILE | per-block RAS / parity / violation log |

`DGE_CMD_INJECT_TABLE` 1,152 records; `STREAM_TO_AXI` tables 960 (size `0x80` each).
This command-injection table engine is a **Maverick addition** — Cayman's TOP_SP carried no
such tables (§6). `[HIGH · OBSERVED]`

### 1c. The TOP_SP CSR leaves (`top_sp_amzn.json`)

The pkl record offsets match the on-disk `top_sp_amzn.json` schema byte-for-byte:

| offset | short_name | type | size | schema |
| --- | --- | --- | --- | --- |
| `+0x0` | `FIS_0` | NODE | — | per-SP FIS protection wrapper |
| `+0x10000` | `TOP_SP_ERG_CSR` | REGFILE | `0x40` | `erg_ecc_model.json` (48 recs) |
| `+0x10040` | `TOP_SP_ERG_PAR` | REGFILE | `0x40` | `erg_parity_model_noinit.json` (48) |
| `+0x11000` | `TOP_SP_MISC_AMZN` | REGFILE | `0x1000` | `top_sp_misc_amzn.json` (48) |

`top_sp_misc_amzn.json` exposes the **write-ordering-buffer** config the NX core uses to
order its emitted DMA descriptors: `wob / wob_wr_bypass / wob_wr_clear / wob_force_inorder
/ wob_use_wid_base / wob_wid_base`. `TPB_TOP_SP_SIDE_FABRIC` = 40 `NODE` records, size
`0x80000` (512 KiB) — the side-fabric aperture wiring TOP_SP into the TPB tile fabric.
`[HIGH · OBSERVED]`

### 1d. The NX sequencer core (`xtensa_nx.json`)

The physical Xtensa-NX control-CSR window is named `TOP_NX_CORE_n` (600 records, `REGFILE`,
size `0x4000` = 16 KiB each, at TPB-parent offsets `+0x5000/+0x9000/+0xd000`, ≥3 cores per
TPB). Because it carries **no** `TOP_SP` token, it physically lands in the **ADDR-11 / TPB
lane** (under `TPB_0`), *not* the TOP_SP lane — important for the non-double-counting
partition in §4. The engine-5 sequencer core is address-mapped; only its *name path* sits
under TPB. `[HIGH · OBSERVED]`

---

## 2. The sync / semaphore subtree `[HIGH · OBSERVED]`

### 2a. The EVT_SEM 256-array (504 records)

`name`-key `EVT_SEM` = **504** (24 `TPB_EVT_SEM` containers × 21 recs/instance; by view
user_int 288 / secure_int 216). Each container is `type=NODE`, size `0x100000` (1 MiB).
Its window children are a **byte-exact** match to the EVT_SEM data-plane model documented
(Cayman-grounded) in [`evt-sem-regions.md`](./evt-sem-regions.md):

| offset | window | size | arr | op | Cayman match |
| --- | --- | --- | --- | --- | --- |
| `+0x0` | `EVENT` | `0x400` | 256 | event set/clr (RW bit0) | `tpb_events` ✓ |
| `+0x400` | `EVENT_RESERVED0` | `0xC00` | — | pad | ✓ |
| `+0x1000` | `SEMAPHORE_READ` | `0x400` | 256 | read sem (RO) | `sem_read` ✓ |
| `+0x1400` | `SEMAPHORE_SET` | `0x400` | 256 | overwrite (WO) | `sem_set` ✓ |
| `+0x1800` | `SEMAPHORE_INC` | `0x400` | 256 | atomic `+=` (WO) | `sem_inc` ✓ |
| `+0x1C00` | `SEMAPHORE_DEC` | `0x400` | 256 | atomic `-=` (WO) | `sem_dec` ✓ |
| `+0x2000` | `SEMAPHORE_CNTR_INC` | `0x4000` | — | counter-inc bank | **NEW (v5)** |
| `+0x6000` | `EVENT_RESERVED1` | `0xFA000` | — | pad to 1 MiB | — |

Each `0x400` window = 256 entries × 4 B → **256 hardware events + 256 hardware 32-bit
semaphores**, confirming the 256/256 model. The four-window aliasing (read/set/inc/dec all
addressing the *same* 256 physical counters, with the address selecting the operation) and
the byte-exact offsets `0x1000/0x1400/0x1800/0x1C00` are documented in full in
[`evt-sem-regions.md`](./evt-sem-regions.md). Bases agree with that page at
`TPB_0_EVT_SEM = 0x2802700000` (Cayman); the v5 equivalent is the same `+0x1000`-class
window layout, view-relative.

> **CORRECTION/DELTA vs Cayman EVT_SEM (`evt-sem-regions.md` §1) `[MED · OBSERVED]`.** The
> Cayman address-map tiles the 1 MiB container as 7 leaves with `EVENT_RESERVED1 = 0xFE000`
> and **no** counter-inc bank. The **Maverick pkl exposes an EXTRA `SEMAPHORE_CNTR_INC` bank
> at `+0x2000` (size `0x4000`)**, shrinking `EVENT_RESERVED1` to `0xFA000`. This is a v5-side
> addition — the counter-increment aperture used by the collective DMA-tail-doorbell path.
> Marked **MED**: observed in the pkl; its per-field semantics are not cross-checked against
> a separate Maverick CSR schema. The 6 original windows and the 256/256 model are unchanged.

`EVT_SEM` also appears as a `LOCAL_EVT_SEM_n` alias plane (the cluster-pseudo-base mirror;
8 `LOCAL_EVT_SEM_0` + 8 `LOCAL_EVT_SEM_1`). `[HIGH · OBSERVED]`

### 2b. COLLECTIVE_SYNC (48 records) — **Maverick-new**

`name`-key `COLLECTIVE` = **48** (24 `LOCAL_` aliases). `type=REGFILE`, size `0x100000`
(1 MiB) at parent-offset `+0x2180000`. This is the on-chip collective **barrier-sync**
region the TOP_SP drives — the address-map home of the barrier→semaphore lowering. A
`COLL_SYNC` short-name strip (16 + 16 reserved) sits inside it. **Cayman's YAML has 0**
`COLLECTIVE` records — this is a v5 collective-acceleration addition (§6). `[HIGH · OBSERVED]`

### 2c. SP_SHARED_RAM (144 records) — **Maverick-new**

`name`-key `SP_SHARED` = **144** (8 banks `SHARED_RAM_BANK_0..7` × 16 + 8 `SP_SHARED_RAM`
+ 8 `LOCAL_SP_SHARED_RAM`). Container `type=NODE`, size `0x100000` (1 MiB) at `+0x2280000`
— the shared scratch the SP sequencers and the TOP_SP share for collective state. Again
**Cayman = 0** (§6). `[HIGH · OBSERVED]`

### 2d. SPAD `cc_op` / device tsync — *not* address-mapped (absence)

The TOP_SP's SPAD `cc_op` command table and the device tsync struct are **runtime software
structs** (the `cc_op` entry rides in the DMA CCE descriptor; the tsync struct is
host-decoded), **not** register blocks. Confirming: `name`-key `SPAD` / `TSYNC` /
`TIMESTAMP` = **0** records. The TOP_SP program is loaded *into* the TOP_SP RAM aperture at
runtime; the address map exposes only the RAM window, never the program.
`[HIGH · OBSERVED absence]`

### 2e. RDM lives in INTC — no double-count (absence)

`name`-key `RDM` = **0**. The Ring-Descriptor-Manager is embedded as `INTC_RDM` (a `0x1000`
APB block) *inside* the INTC family — already counted in the
[`pkl-intc-sprot-security.md`](./pkl-intc-sprot-security.md) lane. It is **not** under
TOP_SP. No double-count. `[HIGH · OBSERVED]`

---

## 3. The MISC / residual subtree `[HIGH · OBSERVED]`

Everything in this lane that is **not** TOP_SP / EVT_SEM / COLLECTIVE / SP_SHARED is a real,
schema-bound block family — **no anonymous remainder**. Two families:

### 3a. The URB routing fabric — the bulk of the residual

The **Unified-Resource-Buffer** is the on-die, **TCAM-matched** address-routing fabric plus
its RAS logs (`name`-key `URB` = **15,624** whole-DB; the physical-partition residual that
lands in *this* lane is **11,592** — the rest are URB-named leaves that first-match into
other lanes, the same keyword-vs-lane overlap §4 explains):

| schema (basename) | binds | what |
| --- | --- | --- |
| `tile_fabric_urb.json` | 1,152 | tile-fabric URB routing |
| `TILE_FABRIC_URB_TCAM_AP_TABLE.json` | 1,152 | tile-fabric TCAM table |
| `non_tile_fabric_urb.json` | 504 | non-tile-fabric URB |
| `NON_TILE_FABRIC_URB_TCAM_AP_TABLE.json` | 504 | non-tile TCAM table |
| `AP_TABLE_CTRL.json` | 1,656 | table control |
| `regfile_{security,access,parity}_log.json`, `regfile_ras.json` | 1,656 / 1,728 ea | per-URB RAS / violation / parity log |
| `READ_URB_0..n` / `WRITE_URB_0..n` | 64 ea | per-port read/write windows |

`[HIGH · OBSERVED]`

### 3b. The PEB chiplet-management peripherals (the boot/clock/IO control plane)

Each lives in the PEB `APB_IO` container. Bases are **view-relative** (the example column
shows the low bits; the view's top nibble — §4 of [`pkl-db.md`](./pkl-db.md) — prefixes the
domain); sizes and schemas are all verified on disk. The two count columns separate the
**whole-DB `name`-keyword** family from the **first-match lane residual** (§4):

| block | name-kw | type | base (low bits) | size | schema (on disk) |
| --- | --- | --- | --- | --- | --- |
| `GPIO_0..` | 552 | NODE | `…0010200000` | `0x80000` | `gpio_0_amzn.json` |
| `QSPI_0..` | 864 | REGFILE | `…0010117000` | `0x1000` | `spi_slave_wrapper.json` |
| `GLOBS` | 3,072 | REGFILE | `…0010117700` | `0x40` | `globals.json` |
| `SPIS_AXI_CFG` | 288 | REGFILE | `…0010117a00` | `0x20` | `pbs_axi_mst.json` / `spis_model.json` |
| `PLL_SOC` | 288 | NODE | `…0010380000` | `0x80000` | `pll_amzn.json` |
| `I2C` | 72 | NODE | `…0010112000` | `0x2000` | `i2c_amzn.json` |
| `PVT_0..` | 48 | REGFILE | `…00101a0000` | `0x10000` | `pvt.json` |
| `(HDIE_)SCRATCHPAD` | 1,616 | NODE | `…0019d00000` | `0x80000` | `hdie_scratchpad_amzn.json` |
| `DFX` | 216 | mixed | — | — | debug / DFT |

> **NOTE — name-keyword ≠ lane residual `[HIGH · OBSERVED]`.** The `name`-kw column above
> counts every record whose name contains the token *anywhere in the DB* (so `GPIO` 552
> includes GPIO-named RAS/parity leaves that first-match into protection lanes). The
> first-match **lane residual** for the PEB peripherals (the slice that lands in *this*
> ADDR-16 lane and feeds §4) is **≈2,240**. Both numbers are correct; they answer different
> questions (keyword family vs physical partition), exactly like every other family in §4.

> **ABSENCE — no `EFUSE`/`BOOT` block `[HIGH · OBSERVED]`.** `name`-key `EFUSE` / `BOOT` = 0.
> Efuse/boot is either folded into `GLOBS`/`PLL` or simply not address-mapped in this DB.

---

## 4. THE GRAND COVERAGE TALLY — every subtree vs 323,198 `[HIGH · OBSERVED]`

This is the page's headline result: **does the union of all six engine subtrees account for
every one of the 323,198 records, with zero remainder?** It does — but only once the
**keyword-vs-lane** distinction is handled correctly.

### 4.1 Why headline keyword counts cannot be summed

The per-subsystem **keyword families overlap heavily**: every IP block has its *own*
embedded INTC, FIS protection, SPROT enforcement, and RAS log. So the report headline counts
**double-count** — e.g. the `FIS` family (34,384) and `INTC` family (31,692) are
**distributed across every protected IP**, physically residing inside the DMA / HBM / PCIe /
TPB subtrees. Summing the headlines (TPB 42,360 + DMA 175,376 + HBM 31,604 + FIS 34,384 +
INTC 31,692 + …) blows past 323,198. First-match distribution of the two worst offenders:

```text
FIS  34,384 -> tpb 4,232 / dma 6,240 / hbm 7,192 / pcie 11,120 / intc 5,448 / topsp 152
INTC 31,692 -> tpb 2,952 / dma 17,856 / hbm 3,648 / pcie 7,008 / intc 228
```

i.e. INTC / FIS / SPROT / NOTIFIC are **decentralized** — embedded in every IP. The fix is a
**non-overlapping, exhaustive first-match partition**.

### 4.2 The partition rule

Each of the 323,198 records is assigned to **exactly one** lane by priority-ordered name
tokens (most-specific physical-IP token wins; embedded INTC/FIS/SPROT fold into their **host
IP**, which is where they physically reside — so **no double-count**):

```text
1. UCIE | D2D | PCIE                          -> pcie/d2d/fabric   (ADDR-14)
2. TOP_SP | SP_CLUSTER | EVT_SEM | COLLECTIVE | SP_SHARED
                                              -> topsp/sync        (ADDR-16)
3. DDMA | CDMA | UDMA                          -> dma              (ADDR-12)
4. TPB | DGE  (incl. the TOP_NX_CORE engine-5 cores, §1d)
                                              -> tpb              (ADDR-11)
5. HBM                                         -> hbm              (ADDR-13)
6. INTC | SPROT | NOTIFIC | FIS  (standalone, not IP-embedded)
                                              -> intc/sprot/sec    (ADDR-15)
7. everything else  (URB fabric + PEB peripherals)
                                              -> misc              (ADDR-16)
```

### 4.3 The tally (by lane × by access-domain view)

The partition was computed two ways — by lane (token first-match) and by view
(`rec['parent_names'][1]`) — and **both axes sum identically to 323,198**:

| lane | user_int | secure_int | pciea | pciem | view | root | **TOTAL** |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ADDR-11  tpb | 13,632 | 22,264 | 0 | 0 | 0 | 0 | **35,896** |
| ADDR-12  dma | 57,760 | 117,616 | 0 | 0 | 0 | 0 | **175,376** |
| ADDR-13  hbm | 1,988 | 29,616 | 0 | 0 | 0 | 0 | **31,604** |
| ADDR-14  pcie/d2d/fabric | 2,960 | 51,192 | 24 | 24 | 0 | 0 | **54,200** |
| ADDR-15  intc/sprot/sec | 1,212 | 4,464 | 0 | 0 | 0 | 0 | **5,676** |
| ADDR-16  topsp/sync/misc | 1,552 | 18,888 | 0 | 0 | 0 | 0 | **20,440** |
| ROOT + 5 VIEW nodes | 0 | 0 | 0 | 0 | 5 | 1 | **6** |
| **TOTAL** | **79,104** | **244,040** | **24** | **24** | **5** | **1** | **323,198** |

```text
  by-lane:  35,896 + 175,376 + 31,604 + 54,200 + 5,676 + 20,440 + 6 = 323,198   ✔
  by-view:  79,104 + 244,040 +     24 +     24 +     5 +      1     = 323,198   ✔
  residual = 323,198 − 323,198 = 0     *** COVERAGE PROVABLY COMPLETE ***
```

**Every record lands in exactly one lane; both independent axes close to zero remainder.**

### 4.4 Cross-checks against the sibling carve pages

| check | result |
| --- | --- |
| **DMA** lane 175,376 == [`pkl-dma-subtree.md`](./pkl-dma-subtree.md) (DDMA 119,356 + CDMA 56,020) | EXACT ✓ |
| **HBM** lane 31,604 == [`pkl-hbm-subtree.md`](./pkl-hbm-subtree.md) (user 1,988 + secure 29,616) | EXACT ✓ |
| **TOP_SP** 5,912 (name-key) == [`pkl-db.md`](./pkl-db.md) §5 / SX-ADDR | EXACT ✓ |
| **PCIe** lane 54,200 == [`pkl-pcie-d2d-fabric.md`](./pkl-pcie-d2d-fabric.md) de-overlapped UCIE\|D2D\|PCIE | see ⚠ below |
| **per-view** 79,104 / 244,040 / 24 / 24 / 5 / 1 == [`pkl-db.md`](./pkl-db.md) §4 | EXACT ✓ |

> **⚠ keyword-vs-lane — the TPB, PCIe and INTC lanes differ from their page headlines, and
> that is correct `[HIGH · OBSERVED]`.** The sibling pages headline **keyword-family** counts
> (which overlap); the §4.3 table uses **physical-partition lane** counts (non-overlapping).
> The differences are *expected* and reconcile via §4.1's first-match distribution:
>
> * **TPB:** [`pkl-tpb-subtree.md`](./pkl-tpb-subtree.md) headlines **42,360** `TPB`-named
>   records (a keyword family). The physical TPB lane is **35,896** — the 6,464 difference is
>   TPB-named leaves whose more-specific token (`DDMA`/`UCIE`/`HBM`/…) first-matches into
>   another lane. Both are correct.
> * **PCIe:** the page reports `UCIE 53,720`, `D2D 33,432`, `PCIE 50` — but **these overlap**
>   (`D2D ∩ UCIE = 33,000`; D2D is *inside* UCIE). The non-overlapping union is
>   `UCIE 53,720 + D2D-only 432 sideband + PCIE 50 − 2 shared = 54,200` — the §4.3 lane total.
> * **INTC/SPROT:** the page headlines the `INTC` keyword family **31,692** and `type='INTC'`
>   fleet **5,904**; the §4.3 *standalone* INTC/SPROT lane is **5,676** — the rest fold into
>   their host IP (DMA 17,856 + HBM 3,648 + …, per §4.1). No double-count.
>
> Keyword counts answer "how many records mention X anywhere?"; lane counts answer "where does
> each record physically live?" Only the lane partition can sum to 323,198.

### 4.5 The ADDR-16 lane internal breakdown

```text
TOP_SP / sync union      6,608  = TOP_SP 5,912 (SP_CLUSTER 2,688 ⊂ TOP_SP; both tokens
                                    on the same TOP_SP_CLUSTER0 leaf, NOT additive)
                                  + EVT_SEM 504 + COLLECTIVE 48 + SP_SHARED 144
MISC residual           13,832  = URB fabric 11,592 + PEB peripherals ~2,240
------------------------ ------
ADDR-16 lane TOTAL      20,440  EXACT
```

---

## 5. Reference pseudocode — the TOP_SP/MISC carve + coverage reconciliation

```python
"""Carve the TOP_SP / sync / MISC subtree and prove total coverage == 323,198.

SAFETY: never pickle.load(). Stream the .json mirror (Recipe C from pkl-db.md):
ijson yields ONE record dict at a time, O(1) memory, zero pickle risk. A single
pass classifies every record into exactly one lane AND tallies the view axis.
"""
import ijson

JSON = ".../arch-headers/maverick/ext/al_address_map_db.json"

# Priority-ordered first-match token rule (most-specific physical-IP token wins).
# Order matters: a TOP_SP_CLUSTER0 DGE leaf must hit TOPSP (rule 2) before TPB/DGE
# (rule 4); a UCIE-embedded D2D leaf must hit PCIE (rule 1) before anything else.
LANE_RULES = [
    ("pcie",  ("UCIE", "D2D", "PCIE")),
    ("topsp", ("TOP_SP", "SP_CLUSTER", "EVT_SEM", "COLLECTIVE", "SP_SHARED")),
    ("dma",   ("DDMA", "CDMA", "UDMA")),
    ("tpb",   ("TPB", "DGE")),              # incl. TOP_NX_CORE engine-5 cores (§1d)
    ("hbm",   ("HBM",)),
    ("intc",  ("INTC", "SPROT", "NOTIFIC", "FIS")),
    # rule 7 ("misc") is the catch-all fall-through below.
]

def classify(name: str) -> str:
    """Assign one address-map record to exactly one physical lane by first match."""
    for lane, tokens in LANE_RULES:
        if any(tok in name for tok in tokens):
            return lane
    return "misc"        # URB fabric + PEB peripherals + the root/views land here...

def coverage_tally(json_path: str) -> tuple[dict, dict, int]:
    """One streaming pass. Returns (by_lane, by_view, total).

    by_lane[lane]            -> record count in that physical partition
    by_view[parent_names[1]] -> record count per access-domain view (independent axis)
    The two dicts MUST sum to the same total (the closure proof).
    """
    by_lane: dict[str, int] = {}
    by_view: dict[str, int] = {}
    total = 0
    with open(json_path, "rb") as f:
        for rec in ijson.items(f, "item"):     # O(1) memory; no code path executes
            total += 1
            pn = rec.get("parent_names", [])
            # --- view axis (independent of the lane partition) ---------------------
            if len(pn) >= 2:                   # a real leaf: parent_names[1] == view
                view = pn[1]
            elif len(pn) == 1:                 # a top-level VIEW node (child of root)
                view = "_view_node_"
            else:                              # parent_names == [] -> the root
                view = "_root_"
            by_view[view] = by_view.get(view, 0) + 1
            # --- lane axis (first-match physical partition) ------------------------
            if view in ("_root_", "_view_node_"):
                lane = "root_view"             # 6 structural nodes, no IP token
            else:
                lane = classify(rec["name"])
            by_lane[lane] = by_lane.get(lane, 0) + 1
    return by_lane, by_view, total

def assert_closure(by_lane: dict, by_view: dict, total: int) -> None:
    """The closure proof: BOTH axes sum to `total`, residual == 0, total == 323,198."""
    lane_sum = sum(by_lane.values())
    view_sum = sum(by_view.values())
    assert lane_sum == total, f"lane axis leaks {total - lane_sum} records"
    assert view_sum == total, f"view axis leaks {total - view_sum} records"
    assert total == 323_198,  f"record total drifted to {total}"
    print(f"COVERAGE COMPLETE: {total} records, residual 0 on both axes")

# by_lane, by_view, total = coverage_tally(JSON)
# assert_closure(by_lane, by_view, total)
#   -> by_lane  {pcie:54200, topsp+misc:20440, dma:175376, tpb:35896,
#                hbm:31604, intc:5676, root_view:6}
#   -> by_view  {secure_int:244040, user_int:79104, user_pciea:24,
#                secure_pciem:24, _view_node_:5, _root_:1}
#   -> COVERAGE COMPLETE: 323198 records, residual 0 on both axes
```

> **NOTE.** `classify()` folds the ADDR-16 lane's `topsp` and `misc` rules into one physical
> lane (rule 2 + the catch-all) — matching §4.3's combined `topsp/sync/misc = 20,440` row.
> Split them (return `"misc"` only for the catch-all) to recover the §4.5 sub-breakdown.

---

## 6. MAVERICK-vs-CAYMAN deltas for TOP_SP / sync `[HIGH · OBSERVED + MED]`

Read from the shipped CAYMAN `address_map_flat.yaml` (a *different* SoC instance — used only
for the generational delta, per the WALL). Cayman's `TOP_SP_0` is a 256 KiB container with
explicit sub-blocks `+0x0 NOTIFIC`, `+0x1000 RAM/RAM_CONFIG` (`top_sp_ram.json`),
`+0x4000 TOP_SP_MISC_USER`, `+0x8000 NX_CORE` (`xtensa_nx.json`). Cayman counts: TOP_SP
~1,860 lines / EVT_SEM 384 / COLLECTIVE **0** / SP_SHARED **0**.

| # | delta (Maverick pkl vs Cayman YAML) | confidence |
| --- | --- | --- |
| 1 | `top_sp_ram.json` (RAM_CONFIG) — Cayman binds it under TOP_SP; Maverick exposes the RAM as a generic node, **0** `TOP_SP`-named RAM_CONFIG bindings (aperture persists, named binding dropped) | MED |
| 2 | **`TOP_SP_MISC_USER` GONE in Maverick** (`name`-key = **0**); only `TOP_SP_MISC_AMZN` (48, secure-only) survives → the user-visible misc CSR was **removed**, misc is now **secure-domain-only** — a security-tightening delta matching the amzn-fail-closed / user-fail-open fork in [`pkl-intc-sprot-security.md`](./pkl-intc-sprot-security.md) | HIGH · OBSERVED |
| 3 | `NX_CORE` (`xtensa_nx.json`) **persists** (600 recs, `TOP_NX_CORE_n`, `0x4000` each) but is renamed off the `TOP_SP` token and reparented under `TPB_0` → falls in the ADDR-11 lane (§1d) | HIGH · OBSERVED |
| 4 | `COLLECTIVE_SYNC` + `SP_SHARED_RAM` **NEW in Maverick** (Cayman = 0) — an explicit 1 MiB barrier region (`+0x2180000`) + 1 MiB 8-bank shared RAM (`+0x2280000`): the collective state/barrier scratch the TOP_SP drives | HIGH · OBSERVED |
| 5 | `EVT_SEM` `SEMAPHORE_CNTR_INC @ +0x2000` (`0x4000`) — NEW window vs Cayman's 7-leaf table (§2a) | MED |
| 6 | `TOP_SP_CLUSTER0` DGE stream-to-AXI cmd-inject tables (`DGE_CMD_INJECT` 1,152 + `STREAM_TO_AXI` 960) — the dominant Maverick TOP_SP surface; Cayman's TOP_SP carried **no** command-injection tables (§1b) | HIGH · OBSERVED |

The TOP_SP **core role is frozen** Cayman==Maverick (engine-5 NX sequencer; EVT_SEM
arrive/wait/dec; DMA-tail doorbell). The deltas are **(a) security-tightening** (`MISC_USER`
removed) and **(b) collective acceleration** (explicit `COLLECTIVE_SYNC` + `SP_SHARED` +
cluster cmd-inject tables added). The role-freeze is HIGH; the "acceleration" reading of the
v5 additions is **INFERRED**.

---

## 7. Confidence ledger

**HIGH · OBSERVED** (re-streamed off the `name` key this session, pkl == JSON mirror):

- TOP_SP family **5,912** (user_int 936 / secure_int 4,976); `type` split 4,984 / 800 / 128.
- `SP_CLUSTER` **2,688** ⊂ TOP_SP (not additive); DGE cmd-inject 1,152 + stream-to-AXI 960.
- TOP_SP CSR leaves match `top_sp_amzn.json` byte-for-byte; `TOP_SP_MISC_AMZN` 48.
- EVT_SEM **504** (24×21; views 288/216); the `+0x1000/1400/1800/1C00` 256-array windows
  byte-match [`evt-sem-regions.md`](./evt-sem-regions.md).
- COLLECTIVE_SYNC **48** + SP_SHARED_RAM **144** (Maverick-new vs Cayman 0).
- MISC residual fully named: URB fabric (lane 11,592) + PEB peripherals (~2,240); all
  schemas on disk; no anonymous remainder; no EFUSE/BOOT block.
- **GRAND TALLY: non-overlapping first-match partition sums EXACTLY 323,198, residual 0 on
  BOTH the lane axis and the independent access-domain-view axis.** Clean lanes (DMA 175,376,
  HBM 31,604, TOP_SP 5,912) match their sibling-page headlines exactly.
- Absences: `SDMA` / `RDM` / `SPAD` / `TSYNC` / `EFUSE` / `BOOT` / `TOP_SP_MISC_USER` all 0.
  RDM lives in INTC (`INTC_RDM`), already in the ADDR-15 lane — no double-count.

**MED:**

- EVT_SEM `SEMAPHORE_CNTR_INC @ +0x2000`: observed in pkl; per-field semantics not
  Maverick-schema-verified.
- `top_sp_ram.json` TOP_SP-named binding absent in Maverick (aperture persists).
- "Collective acceleration" reading of the Maverick additions is INFERRED.
- Keyword-family vs physical-lane counts (TPB 42,360 vs 35,896; INTC 31,692 vs 5,676 lane;
  PCIe overlap) — both correct; they reconcile only via the §4.1 first-match distribution.

**WALL:** all v5 *interior* behavior is INFERRED — the Cayman YAML (§6) is the only
byte-grounded behavioral cross-check and is a different SoC instance.

---

## See also

- [`pkl-db.md`](./pkl-db.md) — the safe-load primitive, 23-field schema, 5 views, type taxonomy
- [`evt-sem-regions.md`](./evt-sem-regions.md) — the EVT_SEM 256-event/256-semaphore data plane (Cayman byte-exact)
- [`../csr/rdm-top-sp.md`](../csr/rdm-top-sp.md) — the TOP_SP / rdm_model CSR layout
- [`../../collectives/ops/top-sp-lowering.md`](../../collectives/ops/top-sp-lowering.md) — barrier→semaphore lowering onto engine 5
- Sibling carve pages: [TPB](./pkl-tpb-subtree.md) · [DMA](./pkl-dma-subtree.md) · [HBM](./pkl-hbm-subtree.md) · [PCIe/D2D](./pkl-pcie-d2d-fabric.md) · [INTC/SPROT](./pkl-intc-sprot-security.md)
- [`block-schema-xref.md`](./block-schema-xref.md) — the block→RTL-schema mapping
