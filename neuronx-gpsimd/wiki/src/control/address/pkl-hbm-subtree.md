# pkl HBM / Memory Subtree

This page carves the **HBM control-plane subtree** out of the Annapurna-Labs
address-map source database (`al_address_map_db.pkl`, MAVERICK / NC-v5) — the
on-die high-bandwidth-memory controller plane: the two **`AMZN_HBM_0`/`AMZN_HBM_1`**
stack controllers, their **32 `HBM_CTRL_DP` datapath controllers** (each a full DDR
controller + scrubber + ECC + traffic-gen assembly), the **DWC HBM PHY**, the
**`HBM_XBAR_8X32`** secure crossbar and the **`HBM_XBAR_4X2`** user crossbar, the
engine-view **RESERVED 128 GiB** placeholder windows, and the FIS/SPROT protection
surface. It reuses the inert load primitive from
[`pkl-db.md`](./pkl-db.md) — **read that first**; this page never re-derives the safe
reader, only the HBM carve.

The **DRAM band itself** (the 4 × 64 GiB Cayman HBM stacks) is *not* in this control
DB — the engine-view HBM node binds `reserved.json` (a decode placeholder); the actual
DRAM is the BAR4 aperture / dynamic-TLB target. This subtree enumerates only the HBM
**CSR / control** plane. The byte-grounded cross-check is the Cayman flat
`HBM_0..3` band in [`soc-master-map.md`](./soc-master-map.md) and
[`unified-soc-memory-map.md`](./unified-soc-memory-map.md); the per-block CSR schemas
are catalogued in [`block-schema-xref.md`](./block-schema-xref.md); the live CSR
register surface in [`../csr/hbm-d2d-pcie-blocks.md`](../csr/hbm-d2d-pcie-blocks.md);
the interrupt/trigger plumbing in
[`../interrupt/pcie-hbm-tpb-d2d-triggers.md`](../interrupt/pcie-hbm-tpb-d2d-triggers.md).

> **WALL — MAVERICK (v5, this DB) vs CAYMAN (NC-v3, cross-check).** The DB lives at
> `arch-headers/`**`maverick`**`/ext/al_address_map_db.pkl`; every record's `json`
> schema path is rooted under `/proj/maverick/…`. So **the DB structure, record names,
> bases, sizes, counts and schema bindings below are `[HIGH · OBSERVED]`** — they are
> read structurally out of the shipped pkl/json (both *are* binary-derived,
> RTL-generated vendor data, and are citeable). **Any claim about what the v5 silicon
> *does* behind one of these addresses is `[* · INFERRED]`.** The byte-grounded
> generational anchor is the **Cayman (NC-v3)** flat band: `HBM_0 @ 0x0`,
> `HBM_1 @ 0x4000000000`, `HBM_2 @ 0x800000000000`, `HBM_3 @ 0x804000000000`, each
> 64 GiB — see [`soc-master-map.md`](./soc-master-map.md) L66/L74/L103. v5 doubles the
> *engine-view* aperture to 128 GiB; the v5 controller interior (PS-pair removal,
> 32-datapath fan-out) is INFERRED from the address map, not from silicon.

---

## 0. Safety — the same inert reader (do **not** `pickle.load`)

A `.pkl` is a program. This 216 MB file must **never** hit `pickle.load()` /
`pickle.loads()` (arbitrary code execution + a 514 MB-graph OOM). Use the
[`pkl-db.md`](./pkl-db.md) primitive: either `pickletools.genops()` (opcode-stream
scan, *no* object construction, *never* calls `find_class`) or stream the `.json`
mirror line-by-line. **This page re-ran both as an independent safety gate** before any
HBM analysis:

| check | value | `[conf · prov]` |
|-------|-------|-----------------|
| header bytes | `80 04 95 08 00 01 00 00 00 00 5d` = PROTO 4 · FRAME · `EMPTY_LIST` | `[HIGH · OBSERVED]` |
| `genops()` opcodes scanned | **33,065,559** | `[HIGH · OBSERVED]` |
| dangerous opcodes `{GLOBAL, STACK_GLOBAL, REDUCE, INST, OBJ, NEWOBJ*, BUILD, EXT*, PERSID, BINPERSID}` | **0** | `[HIGH · OBSERVED]` |
| top opcodes | `MEMOIZE` 10,141,891 · `BINGET` 8,024,869 · `SHORT_BINUNICODE` 7,894,409 | `[HIGH · OBSERVED]` |
| total records (root list `len`) | **323,198** | `[HIGH · OBSERVED]` |

> **NOTE — no drift.** Opcode total (33,065,559), record count (323,198), file size
> (216,631,794 B) and the zero-dangerous-opcode result are byte-identical to the
> [`pkl-db.md`](./pkl-db.md) baseline. The DB is the same artifact; the HBM carve below
> uses the `.json` mirror as a pure-text cross-check (no pickle risk).

---

## 1. Two planes — where the HBM control actually lives

The HBM control surface is split across **two address-map views**, mirroring the DMA
two-plane model: a thin **engine-view placeholder** and the real **secure/privileged
controller plane**.

### 1a. Engine view — `USER_INT_SENG_n_HBM` is a RESERVED 128 GiB stub

Each of the four SENG (Scalar Engine) cores carries exactly one engine-view HBM node,
parent chain `['ADDRESS_MAP','user_int','seng_n']`. All four are `NODE`, `count=1`,
bind `address_map/reserved.json`, and have **zero descendants** — they are pure decode
placeholders, *not* the controllers. `[HIGH · OBSERVED]`

| node | base | size | meaning |
|------|------|------|---------|
| `USER_INT_SENG_0_HBM` | `0x8000000000` | `0x2000000000` (**128 GiB**) | engine-view HBM window stub |
| `USER_INT_SENG_1_HBM` | `0x108000000000` | `0x2000000000` | `= SENG_0 + 0x100000000000` |
| `USER_INT_SENG_2_HBM` | `0x208000000000` | `0x2000000000` | `= SENG_0 + 2·stride` |
| `USER_INT_SENG_3_HBM` | `0x308000000000` | `0x2000000000` | `= SENG_0 + 3·stride` |

SENG stride = **`0x100000000000`** (16 TiB, the engine-aperture stride). The window is
`0x2000000000` = **128 GiB** per SENG. `[HIGH · OBSERVED]`

> **GEN-CHANGE — v5 doubles the engine-view aperture.** The Cayman (v3) flat band
> exposes 4 × **64 GiB** HBM stacks (`soc-master-map.md` L66/L107). The Maverick
> engine-view stub is **128 GiB** per SENG. The DRAM *size* itself is not proven by
> this control DB (the node binds `reserved.json`); 128 GiB is the *decode aperture*
> the v5 engine view reserves. v5 interior DRAM geometry `[MED · INFERRED]`; the
> 128 GiB aperture value `[HIGH · OBSERVED]`.

> **GOTCHA — the user_int "HBM" keyword count (1,988) is *not* these four stubs.** It
> is dominated by the host-visible FIS/SPROT protection surface and the user-view
> `HBM_XBAR_4X2` crossbar (§5), which carry `HBM` in their names but sit under
> `APB_IO`, not under `SENG_n_HBM`. The four stubs contribute only 4 of the 1,988.

### 1b. Secure / privileged plane — `AMZN_HBM_0` / `AMZN_HBM_1` (the real controllers)

The real HBM CSRs live in the **secure_int** view, reached through the **PEB aperture**
on the **HBM die** (`H_DIE`). Parent chain
`['ADDRESS_MAP','secure_int','seng_n','h_die','PEB_APB_IO','amzn_hbm_0'|'amzn_hbm_1', …]`.
Two stack controllers per die: `[HIGH · OBSERVED]`

| node (SENG_0, plain aperture) | base | size | type | `self_array_size` |
|-------------------------------|------|------|------|-------------------|
| `…PEB_APB_IO_AMZN_HBM_0` | `0x2000048010a00000` | `0x3500000` (**53 MiB**) | `NODE` | `2` |
| `…PEB_APB_IO_AMZN_HBM_1` | `0x2000048013f00000` | `0x3500000` | `NODE` | `2` |

The top view nibble `bits[63:60] = 0x2` selects the **secure_int** view. `AMZN_HBM`
lives **only** on `H_DIE` (HBM die); `C_DIE` carries zero `AMZN_HBM` nodes.
`[HIGH · OBSERVED]`

**Dual aperture.** Each `AMZN_HBM_n` surfaces in **two** PEB sub-apertures per SENG —
`PEB_APB_IO` (plain privileged) and `PEB_APB_IO_BCAST` (privileged APB-broadcast). The
BCAST aperture is `+0x20000000` (bit 29) from the plain one: `[HIGH · OBSERVED]`

```
plain  …PEB_APB_IO_AMZN_HBM_0        @ 0x2000048010a00000
bcast  …PEB_APB_IO_BCAST_AMZN_HBM_0  @ 0x2000048030a00000   (= plain + 0x20000000)
```

So **`AMZN_HBM_0` appears 8 times = 4 SENG × {plain, BCAST}** — verified:
`rg -c '"name": "[^"]*_AMZN_HBM_0"'` = **8**; same for `AMZN_HBM_1`. The bulk
controller CSRs are SECURE-only and replicated ×2 apertures, which is why secure_int
HBM records outnumber user_int ~15× (§5). `[HIGH · OBSERVED]`

> **CORRECTION vs SX-ADDR-13 §2b base.** The report cites the `AMZN_HBM_0` plain base
> as `0x2000048010a00000` and the BCAST as `0x2000048030a00000`; both reconfirmed
> byte-exact here (the `AMZN_HBM_1` base `0x2000048013f00000` is added — it was implied
> but not tabulated in the report). No discrepancy; geometry holds.

---

## 2. The stack controller — `AMZN_HBM_0` internal layout

Sample = `SECURE_INT_SENG_0_H_DIE_PEB_APB_IO_AMZN_HBM_0` (base `0x2000048010a00000`,
size `0x3500000` = 53 MiB, `NODE`). `AMZN_HBM_0` and `AMZN_HBM_1` are
**byte-identical in structure** — the two HBM stacks per die. `[HIGH · OBSERVED]`

| offset in stack | block | type | size | schema (`json`) | role |
|-----------------|-------|------|------|-----------------|------|
| `+0x0000000` | `MISC_FIS_0` | `NODE` | `0x010000` | `fis_type_1000_amzn.json` | misc fabric-interface slice |
| `+0x0010000` | `ERRTRIG_INTC` | `NODE` | `0x003000` | `errtrig_amzn.json` | stack error-trigger INTC |
| `+0x0013000` | `A2I` | `REGFILE` | `0x000100` | `a2i_model.json` | APB-to-internal bridge |
| `+0x0014000` | `HBM_TOP_CFG` | `REGFILE` | `0x001000` | `hbm_cfg.json` | top HBM config |
| `+0x0080000` | `PHY_FIS_0` | `NODE` | `0x010000` | `fis_type_1000_amzn.json` | PHY fabric-interface slice |
| `+0x0090000` | `DWC_HBMPHY` | `REGFILE` | `0x2000000` (**32 MiB**) | `dwc_hbmphy_top.json` | Synopsys DWC HBM PHY |
| `+0x2100000` | `HBM_CTRL_DP_0` | `NODE` | `0x080000` | `hbm_ctrl_dp.json` | datapath controller 0 |
| … | `HBM_CTRL_DP_1..30` | `NODE` | `0x080000` | `hbm_ctrl_dp.json` | datapaths 1..30 |
| `+0x3080000` | `HBM_CTRL_DP_31` | `NODE` | `0x080000` | `hbm_ctrl_dp.json` | datapath controller 31 |
| `+0x3100000` | `HBM_XBAR_8X32_FIS_0` | `NODE` | `0x010000` | `fis_type_1101_amzn.json` | xbar FIS slice 0 |
| … | `HBM_XBAR_8X32_FIS_1..7` | `NODE` | `0x010000` | `fis_type_0001_amzn.json` | xbar FIS slices 1..7 |
| `+0x3490000` | `HBM_XBAR_8X32` | `NODE` | `0x020000` | `hbm_xbar_8x32_amzn.json` | the 8×32 crossbar |

The 32 `HBM_CTRL_DP` nodes tile contiguously from `+0x2100000` at a **`0x80000`
(512 KiB) stride**; the `HBM_XBAR_8X32_FIS_n` indices run **0..7** (eight slices,
verified). `[HIGH · OBSERVED]`

### 2a. The `HBM_CTRL_DP` datapath controller (per-channel DDR controller)

Sample = `…AMZN_HBM_0_HBM_CTRL_DP_0` (`NODE`, size `0x80000` = 512 KiB; `count=2` is
the 2-stack multiplicity, `self_array_size="32"` is the 32-datapath array). Direct
children: `[HIGH · OBSERVED]`

| offset in DP | block | type | size | schema | role |
|--------------|-------|------|------|--------|------|
| `+0x00000` | `DP_FIS_0` | `NODE` | `0x010000` | `fis_type_1000_amzn.json` | datapath fabric-interface slice |
| `+0x10000` | `RMBS` | `REGFILE` | `0x010000` | `ddr_csr_apb.json` | **the DDR memory controller** |
| `+0x20000` | `SCBR` | `REGFILE` | `0x001000` | `hbm_scbr.json` | March/BIST scrubber |
| `+0x21000` | `AXTG` | `NODE` | `0x000800` | `trfc_gen_wrapper.json` | traffic generator |
| `+0x22000` | `ERG_0` | `REGFILE` | `0x000040` | `erg_ecc_model.json` | ECC engine 0 |
| `+0x22040` | `ERG_1` | `REGFILE` | `0x000040` | `erg_ecc_model.json` | ECC engine 1 |
| `+0x23000` | `ELA` | `REGFILE` | `0x001000` | `cxela500.json` | ARM CoreSight ELA |
| `+0x24000` | `DP_MISC` | `REGFILE` | `0x001000` | `hbm_ctrl_dp_misc.json` | datapath misc cfg |
| `+0x25000` | `ERRTRIG_INTC` | `NODE` | `0x003000` | `errtrig_amzn.json` | datapath error-trigger INTC |
| `+0x30000` | `RESERVED` | `NODE` | `0x050000` | `reserved.json` | decode reserved |

- **`RMBS`** = the `ddr_csr_apb` DDR controller — `GenFlavor=EXTERNAL_IP`,
  `SizeInBytes=0x10000`, a Cadence/Denali-class APB DDR controller (the
  ECC/parity/thermal/DFI surface). `[HIGH · OBSERVED]`
- **`SCBR`** = the `hbm_scbr` March/BIST scrubber (per datapath). `[HIGH · OBSERVED]`
- **`ERG_0`/`ERG_1`** = the `erg_ecc_model` ECC engines (2 per datapath).
  `[HIGH · OBSERVED]`
- **`AXTG`** = the `trfc_gen_wrapper` traffic generator. Its interior carries a full
  `CFG_APB_CFG_ENG_SEQ_TABLE`, `AXI_OS_SHAPER`, `USER_DATA_TABLE`, an `INTC` and a
  `RAS` (access/security/parity violation) sub-surface. `[HIGH · OBSERVED]`
- **`ELA`** = the `cxela500` CoreSight Embedded Logic Analyzer (debug). `[HIGH · OBSERVED]`

The block→schema name mapping (`RMBS`/`SCBR`/`AXTG`/`ELA` → the same schema basenames)
matches the CSR catalogue in [`../csr/hbm-d2d-pcie-blocks.md`](../csr/hbm-d2d-pcie-blocks.md)
exactly. `[HIGH · OBSERVED]`

### 2b. The `HBM_XBAR_8X32` crossbar — geometry is byte-grounded

Sample = `…AMZN_HBM_0_HBM_XBAR_8X32` (`NODE`, size `0x20000` = 128 KiB,
`hbm_xbar_8x32_amzn.json`). Its `AMZN` child enumerates the actual port array, which
**resolves the "8X32" name to silicon-side port counts** rather than leaving it as a
name inference: `[HIGH · OBSERVED]`

| port family | indices present | count | side |
|-------------|-----------------|-------|------|
| `…_AMZN_PORT_FAB_RD_n` | 0..7 | **8** | fabric (client) read |
| `…_AMZN_PORT_FAB_WR_n` | 0..7 | **8** | fabric (client) write |
| `…_AMZN_PORT_HBM_RD_n` | 0..31 | **32** | HBM-side (datapath) read |
| `…_AMZN_PORT_HBM_WR_n` | 0..31 | **32** | HBM-side (datapath) write |

So **`8X32` = 8 fabric-side client ports × 32 HBM-side datapath ports** — the crossbar
fans 8 SoC clients onto the 32 `HBM_CTRL_DP` datapaths (the address-hash /
channel-interleave remapper). The `AMZN` block also carries `F2H_CTRL` (fabric→HBM) and
`H2F_CTRL` (HBM→fabric) direction-control regfiles. `[HIGH · OBSERVED]`

> **CORRECTION (upgrade) vs SX-ADDR-13 §3b.** The report tagged the
> "8 client ports × 32 datapaths" routing as `[MED]` — *"read from the name + the
> 32-datapath count"*. Re-streaming the `HBM_XBAR_8X32_AMZN` port array shows
> `PORT_FAB_{RD,WR}_0..7` (8) and `PORT_HBM_{RD,WR}_0..31` (32) **explicitly enumerated
> in the address map**, so the 8×32 geometry is `[HIGH · OBSERVED]`, not inferred. The
> *routing policy* (which client maps to which datapath) remains a firmware/RTL
> question, but the port topology is byte-grounded.

---

## 3. Channel / stack geometry + block census

**Stack model** `[HIGH · OBSERVED]`:

- **2 HBM stacks per die**: `AMZN_HBM_0` + `AMZN_HBM_1`, byte-identical structure, on
  `H_DIE` only.
- **4 SENG × 2 apertures = 8 container instances** of each stack node.
- **Per stack: 32 `HBM_CTRL_DP` datapath controllers** (`self_array_size="32"`,
  512 KiB stride). DP indices run **0..31** (verified `max = 31`); no `_32` exists.
- The "channel" dimension that Cayman expressed as a CSR array
  (`hbm_cfg.hbm_ctrl_debug ArraySize=16`) is, in Maverick, the **32 explicit
  `HBM_CTRL_DP` address-map nodes** per stack — the channel array migrated from a CSR
  array into the address map (§4).

**Whole-HBM-subtree block census.** Two scopes matter and are distinguished below to
avoid a double-counting trap: *stack-scoped* (record `name` contains `AMZN_HBM` — the
real per-stack controllers) vs *HBM-family-wide* (record `name` contains `HBM` —
includes the user-plane crossbar + FIS/SPROT). Counts are streamed from the `.json`
mirror by the record's `json` schema field: `[HIGH · OBSERVED]`

| schema (`json` basename) | binds → | stack-scoped (`AMZN_HBM`) | what |
|--------------------------|---------|---------------------------|------|
| `ddr_csr_apb.json` | `RMBS` | **512** | DDR memory controller (EXTERNAL_IP) |
| `hbm_ctrl_dp.json` | `HBM_CTRL_DP` | **512** | datapath-controller container `NODE` |
| `hbm_scbr.json` | `SCBR` | **512** | March/BIST scrubber (1/datapath) |
| `hbm_ctrl_dp_misc.json` | `DP_MISC` | **512** | datapath misc cfg |
| `trfc_gen_wrapper.json` | `AXTG` | **512** | per-datapath traffic generator |
| `erg_ecc_model.json` | `ERG_0`/`ERG_1` | **1024** | ECC engine (2/datapath) |
| `cxela500.json` | `ELA` | **512** | CoreSight ELA (1/datapath) |
| `dwc_hbmphy_top.json` | `DWC_HBMPHY` | **16** | DWC HBM PHY (32 MiB; 1/stack) |
| `hbm_cfg.json` | `HBM_TOP_CFG` | **16** | top HBM config (1/stack) |
| `hbm_xbar_8x32_amzn.json` | `HBM_XBAR_8X32` | **16** | secure 8×32 crossbar (1/stack) |
| `a2i_model.json` | `A2I` | **16** | APB-to-internal bridge (1/stack) |
| `hbm_xbar_4x2_amzn.json` | `HBM_XBAR_4X2` | **0** (HBM-family-wide: **64**) | user/tile-fabric 4×2 crossbar |

**Controller-count arithmetic** `[HIGH · OBSERVED]`:

```
512 RMBS (ddr_csr_apb) = 32 datapaths × 16 AMZN_HBM container-instances
   16 = AMZN_HBM_0 (8 inst) + AMZN_HBM_1 (8 inst);  8 = 4 SENG × 2 apertures
16 DWC_HBMPHY = 16 hbm_cfg = 16 HBM_XBAR_8X32 = 16 A2I
             = 2 stacks × 8 apertures = 1 per (stack × aperture)
```

> **CORRECTION vs SX-ADDR-13 §4 census (two scoping fixes).** The report's §4 table
> mixes the two scopes for two rows. (1) `cxela500.json` is listed as **704**
> *"datapaths + xbar + stack"* — that is the HBM-family total; the **AMZN_HBM
> stack-subtree carries exactly 512** (one ELA per datapath, `512 = 32 × 16`). The
> remaining 192 ELAs (704 − 512) live in the FIS/SPROT orphan surface (§5), not under
> `AMZN_HBM`. (2) `hbm_xbar_4x2_amzn.json` is listed as **64** in the census; that count
> is correct **only HBM-family-wide** — **zero** of those 64 carry `AMZN_HBM` in their
> name. The 4×2 crossbar is the **user/tile-fabric** crossbar under
> `amzn_tile_fabric_0..3` (one per tile fabric), *not* part of the per-stack
> `AMZN_HBM` controller. Both numbers from the report are reproducible; the page above
> labels each with its correct scope so the two cannot be conflated.

> **GOTCHA — `trfc_gen_wrapper`/`erg_ecc_model`/`cxela500` are shared schemas.** Counted
> whole-DB they are 800 / 1704 / 2056 respectively (they bind non-HBM nodes too). Only
> the `AMZN_HBM`-name-scoped subset (512 / 1024 / 512) belongs to the HBM stacks. Never
> quote the whole-DB schema-bind count as an HBM controller count.

---

## 4. The FIS / SPROT protection surface over HBM

Of the 29,616 secure-plane HBM records, **5,088 are *not* under `AMZN_HBM_*`** — they
are the per-stack Fabric-Interface-Slice (FIS) protection plus the user-visible
`HBM_XBAR_4X2` crossbar, the same 3-view (host-FIS / priv-FIS / priv-BCAST-FIS)
protection decomposition the DMA subtree exhibits. By context: `[HIGH · OBSERVED]`

| parent context | records | what |
|----------------|---------|------|
| `PEB_APB_IO.user.fis_hbm_0` | 928 | priv FIS, HBM stack 0 |
| `PEB_APB_IO.user.fis_hbm_1` | 928 | priv FIS, HBM stack 1 |
| `PEB_APB_IO_BCAST.user.fis_hbm_0` | 928 | priv-bcast FIS, stack 0 |
| `PEB_APB_IO_BCAST.user.fis_hbm_1` | 928 | priv-bcast FIS, stack 1 |
| `amzn_tile_fabric_0..3.hbm_xbar_4x2` | 60 each (240) | the 4×2 user crossbar (4 fabrics) |
| (`PAPB_BCAST` / `RESERVED_REGION0` / `QOS` / `ELA` / `CRC_HASH` / `NTS`) | remainder | protection/QoS/debug |

Top `short_name`s in this orphan set: `PAPB_BCAST` 672, `RESERVED_REGION0` 544,
`DP_FIS_0` 512, `DP_DEBUG_FIS_0` 512, `FIS_0` 152, `HBM_XBAR_4X2` 128, `NTS` 128,
`QOS` 128, `ELA` 128. This FIS/SPROT layer is the access-protection wrapper around the
HBM CSR aperture; its `RAS` (access/security/parity violation) registers are the same
family seen inside `AXTG` (§2a). `[HIGH · OBSERVED]`

---

## 5. Record-arithmetic closure (closes to the byte)

Whole-DB HBM family = **31,604** records (pkl == JSON mirror, both verified by
`rg -c '"name": "[^"]*HBM'`). `[HIGH · OBSERVED]`

```
SECURE_INT  (29,616) — the real controllers + protection
   AMZN_HBM_0 subtree    12,264   (8 container-instances × 1,533 records/inst)
   AMZN_HBM_1 subtree    12,264   (8 container-instances × 1,533 records/inst)
   FIS/SPROT + user-xbar  5,088   (§4 — NOT under AMZN_HBM_*)
   ----------------------------------------------------------------
   TOTAL                 29,616   EXACT

USER_INT    (1,988) — host-visible view
   4 SENG HBM reserved stubs (§1a; 0 descendants)        4
   host-visible FIS/SPROT + HBM_XBAR_4X2 + fragments  1,984   (h_die 1,888; c_die 96)
   ----------------------------------------------------------------
   TOTAL                                              1,988   EXACT

GRAND:  29,616 + 1,988 = 31,604   EXACT
```

Per-`AMZN_HBM_0` instance = **1,533** records (1 container + 1,532 descendants):

```
HBM_CTRL_DP_*        1,408   (32 datapaths × 44 records/datapath)
HBM_XBAR_8X32           84
HBM_XBAR_8X32_FIS_*     29
ERRTRIG_INTC             4
MISC_FIS_0 / PHY_FIS_0   2 each
A2I / HBM_TOP_CFG / DWC_HBMPHY   1 each
   1408 + 84 + 29 + 4 + 2 + 2 + 1 + 1 + 1 = 1,532 desc;  + 1 container = 1,533   EXACT
```

Both top-level scopes reproduced live: `rg -c '"name": "SECURE_INT[^"]*HBM'` = 29,616;
`rg -c '"name": "USER_INT[^"]*HBM'` = 1,988; sum 31,604. `[HIGH · OBSERVED]`

### JSON-sibling equivalence (representative record)

| field | pkl record | JSON mirror | match |
|-------|------------|-------------|-------|
| `name` | `SECURE_INT_SENG_0_H_DIE_PEB_APB_IO_AMZN_HBM_0_HBM_CTRL_DP_0_RMBS` | same | ✓ |
| `base` | `0x2000048012b10000` | `2305847957329608704` (= `0x2000048012b10000`) | ✓ byte-exact |
| `size` | `0x10000` | `65536` | ✓ |
| `type` | `REGFILE` | `REGFILE` | ✓ |
| `offset` | `0x10000` | `65536` | ✓ |
| `json` | `…/csrs/hbm/ddr_csr_apb.json` | same | ✓ |

The Maverick `ddr_csr_apb.json` schema confirms `GenFlavor=EXTERNAL_IP`,
`SizeInBytes=0x10000`, **19 register-bundle arrays, no PS0/PS1 pseudo-channel bundles**
(the Cayman schema has **27** bundle arrays). `[HIGH · OBSERVED]`

---

## 6. Maverick (v5) vs Cayman (v3) — the generational delta

| aspect | CAYMAN (v3, byte-grounded cross-check) | MAVERICK (v5, this pkl) | `[conf]` |
|--------|----------------------------------------|--------------------------|----------|
| stacks/package | 4 × 64 GiB (`HBM_0..3` flat band) | 2/die (`AMZN_HBM_0/1`) × 2 dies = 4 | `[HIGH]` |
| DRAM band in this DB | flat YAML had `HBM_0..3` DRAM leaves | **NOT present** — engine-view binds `reserved.json` (128 GiB stub); DRAM = BAR4/TLB | `[HIGH]` |
| stack-ctrl aperture | CSR blocks in BAR0 control plane | `AMZN_HBM_n` `NODE`, **53 MiB** per stack | `[HIGH]` |
| channel/datapath array | `hbm_cfg.hbm_ctrl_debug ArraySize=16` (16-channel CSR array) | **32 × `HBM_CTRL_DP`** address-map nodes/stack | `[HIGH]` |
| `ddr_csr_apb` (DDR ctrl) | **27** bundle arrays, 8 PS0/PS1 pseudo-channel pairs | **19** bundle arrays, **no PS pairs** (single-channel) | `[HIGH]` |
| `ddr_csr_apb` GenFlavor | `EXTERNAL_IP` | `EXTERNAL_IP` — **same** | `[HIGH]` |
| DWC HBM PHY | `dwc_hbmphy_top` (273 bundles / 32 MiB) | `dwc_hbmphy_top` (512 bundles / 32 MiB) | `[HIGH]` |
| scrubber | `hbm_scbr` | `hbm_scbr` — **same** (per datapath) | `[HIGH]` |
| crossbar | `hbm_xbar_cfg` (addr-translate + RAS) | `HBM_XBAR_8X32` (secure, **8 fab × 32 hbm**) + `HBM_XBAR_4X2` (user) | `[HIGH]` |
| post-package-repair | `hbm_hpr` (page-retirement) | **no `hbm_hpr` node** in HBM subtree | `[MED]` (absent vs relocated unresolved) |
| ECC engine | `ddr_csr_apb ECC_CONFIG` + on-die | + `erg_ecc_model` (`ERG`, 1024 in subtree) | `[HIGH]` |
| priv aperture | bit-53 `PEB_APB_IO` | `PEB_APB_IO` **+** `PEB_APB_IO_BCAST` (dual; `+0x20000000`) | `[HIGH]` |
| traffic generator | (not a named HBM block) | `AXTG` (`trfc_gen_wrapper`, **new**) | `[HIGH]` |

**Net:** same HBM IP family (DDR controller + DWC PHY + scrubber + crossbar + ECC/RAS),
re-organized for v5 — the PS0/PS1 pseudo-channel split was removed from the DDR
controller, the 16-channel CSR array was replaced by 32 explicit `HBM_CTRL_DP` nodes,
the crossbar grew to a secure 8×32 fabric (plus a user 4×2 fabric), and a per-datapath
traffic generator (`AXTG`) was added. The DRAM band is decode-reserved in this control
DB. `[HIGH where byte-exact; the `hbm_hpr` absent-vs-relocated reading is MED]`

> **NOTE — negative anchors hold.** No `PREPROC`-named node carries HBM (PREPROC is a
> pure Q7 cluster, no HBM); the on-chip SBUF/PSUM/scratch SRAM are TPB-engine regions,
> not HBM, and never appear in this subtree. This DB's "memory subtree" is the HBM
> **control** plane only; the on-chip SRAM and the HBM DRAM heap are accounted
> elsewhere ([`unified-soc-memory-map.md`](./unified-soc-memory-map.md)).
> `[HIGH · OBSERVED]`

---

## 7. Reimplementation — carve + per-controller base derivation

The carve never constructs a pickle object: it streams the `.json` mirror line-by-line,
tracking the current record `name`, and filters to the HBM subtree by name prefix. (To
work off the pkl directly instead, materialize via the guarded `RestrictedUnpickler`
from [`pkl-db.md`](./pkl-db.md) — *never* a bare `pickle.load`.)

```python
import json, re
from collections import Counter

JSON = ".../arch-headers/maverick/ext/al_address_map_db.json"  # 514 MB pretty mirror

# Stream the pretty-printed array WITHOUT slurping: the mirror emits one field per
# line, so a record's "name" line precedes its scalar fields. We never json.load() the
# whole file; we accumulate one record dict at a time, keyed off the "name" line.
NAME = re.compile(r'^\s*"name": "([^"]*)"')
FLD  = re.compile(r'^\s*"(base|size|offset|type|count|self_array_size|json)": (.+?),?\s*$')

def stream_records(path):
    """Yield {name, base, size, type, json, ...} dicts for the HBM subtree only."""
    rec, name = None, None
    with open(path) as f:
        for line in f:
            m = NAME.match(line)
            if m:                                  # new record boundary
                if rec is not None and "HBM" in rec.get("name", ""):
                    yield rec
                name = m.group(1)
                rec = {"name": name}
                continue
            fm = FLD.match(line)
            if fm and rec is not None:
                k, v = fm.group(1), fm.group(2).strip().strip('"')
                rec[k] = int(v, 0) if k in ("base", "size", "offset") and v.lstrip("-").isdigit() else v
        if rec is not None and "HBM" in rec.get("name", ""):
            yield rec

# --- counts (re-grounded by streaming, never grep-from-decompile) ---
hbm_total      = 0
by_schema      = Counter()          # AMZN_HBM-scoped schema census
secure, user   = 0, 0
for r in stream_records(JSON):
    hbm_total += 1
    if r["name"].startswith("SECURE_INT"): secure += 1
    if r["name"].startswith("USER_INT"):   user   += 1
    if "AMZN_HBM" in r["name"] and "json" in r:
        by_schema[r["json"].rsplit("/", 1)[-1]] += 1

assert hbm_total == 31_604                       # HBM family
assert (secure, user) == (29_616, 1_988)         # plane split
assert by_schema["ddr_csr_apb.json"]   == 512    # the 512 DDR controllers
assert by_schema["hbm_scbr.json"]      == 512    # 512 scrubbers
assert by_schema["erg_ecc_model.json"] == 1024   # 2 ECC engines × 512 datapaths
assert by_schema["dwc_hbmphy_top.json"]== 16     # 16 DWC PHYs (1 / stack-instance)
assert by_schema["hbm_xbar_8x32_amzn.json"] == 16

# --- per-controller (RMBS) base derivation from the stack base ---
# AMZN_HBM_0 (SENG_0, plain aperture) container base, OBSERVED:
STACK_BASE      = 0x2000048010a00000
DP_BASE_OFF     = 0x2100000          # HBM_CTRL_DP_0 offset within the stack
DP_STRIDE       = 0x80000            # 512 KiB per datapath; indices 0..31
RMBS_OFF        = 0x10000            # RMBS (ddr_csr_apb) offset within a datapath
BCAST_DELTA     = 0x20000000         # PEB_APB_IO -> PEB_APB_IO_BCAST aperture

def rmbs_base(stack_base, dp, bcast=False):
    """Base of the ddr_csr_apb DDR controller for datapath `dp` (0..31)."""
    assert 0 <= dp < 32
    b = stack_base + DP_BASE_OFF + dp * DP_STRIDE + RMBS_OFF
    return b + (BCAST_DELTA if bcast else 0)

assert rmbs_base(STACK_BASE, 0)  == 0x2000048012b10000   # == the OBSERVED §5 record
assert rmbs_base(STACK_BASE, 0, bcast=True) == 0x2000048032b10000
```

The `assert`s are the page's self-test: every constant is `[HIGH · OBSERVED]` from the
streamed DB, and `rmbs_base(STACK_BASE, 0)` reproduces the byte-exact `RMBS` base
recorded in the JSON mirror (§5). `[HIGH · OBSERVED]`

---

## 8. Confidence ledger

**`[HIGH · OBSERVED]`** — read byte-exact from the shipped pkl/json + on-disk schemas:

- Safe-load inert (0 dangerous opcodes / 33,065,559 scanned / `len` 323,198; no drift).
- HBM family **31,604** (secure 29,616 + user 1,988); pkl == JSON mirror.
- Engine-view `USER_INT_SENG_n_HBM` = RESERVED **128 GiB** stub (size `0x2000000000`,
  base `0x8000000000`, stride `0x100000000000`, binds `reserved.json`, 0 descendants).
- **2 HBM stacks** `AMZN_HBM_0/1` (byte-identical), `H_DIE` only, **8 instances each**
  (4 SENG × {`PEB_APB_IO`, `PEB_APB_IO_BCAST`}, BCAST `+0x20000000`), 53 MiB aperture.
- Stack layout: `MISC_FIS` / `ERRTRIG` / `A2I` / `HBM_TOP_CFG`(`hbm_cfg`) / `PHY_FIS` /
  `DWC_HBMPHY`(32 MiB) / **32 × `HBM_CTRL_DP`** (512 KiB stride) / `HBM_XBAR_8X32` + 8 FIS.
- Datapath = `RMBS`(`ddr_csr_apb`) + `SCBR`(`hbm_scbr`) + `AXTG`(`trfc_gen`) + `ERG_0/1`
  + `ELA`(`cxela500`) + `DP_MISC` + `ERRTRIG` + `RESERVED`.
- **`HBM_XBAR_8X32` = 8 fabric ports (`PORT_FAB_{RD,WR}_0..7`) × 32 HBM ports
  (`PORT_HBM_{RD,WR}_0..31`)** — enumerated, not inferred.
- Stack-scoped census: 512 `ddr_csr_apb`, 512 `hbm_ctrl_dp`, 512 `hbm_scbr`, 512
  `hbm_ctrl_dp_misc`, 512 `trfc_gen_wrapper`, 1024 `erg_ecc_model`, 512 `cxela500`, 16
  `dwc_hbmphy_top`, 16 `hbm_cfg`, 16 `hbm_xbar_8x32_amzn`, 16 `a2i_model`.
- Maverick `ddr_csr_apb` = 19 bundle arrays, no PS pairs, `EXTERNAL_IP` vs Cayman 27.
- Record arithmetic closes EXACTLY (12,264 + 12,264 + 5,088 = 29,616; 4 + 1,984 = 1,988;
  grand 31,604); `RMBS` JSON base == pkl base byte-exact.

**`[MED]`**:

- `hbm_hpr` (post-package-repair) absent in the HBM subtree is OBSERVED; whether v5
  dropped or relocated it is INFERRED.
- The crossbar *routing policy* (client→datapath address hash) is firmware/RTL; only the
  port *topology* is OBSERVED.

**`[* · INFERRED]` (v5 interior)**:

- The 64 GiB-per-stack DRAM *size* is carried from the Cayman flat band; this control DB
  proves 4 stacks and the 128 GiB engine-view aperture, not the v5 DRAM size.
- Whether v5 firmware treats the 32 `HBM_CTRL_DP` as 32 logical channels or
  16-channel × 2-pseudo is a firmware question; the address map shows 32 nodes.
