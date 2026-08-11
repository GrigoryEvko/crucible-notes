# pkl PCIe / D2D / Fabric Subtree

The Maverick host-interface and inter-die interconnect, carved from the shipped
[`al_address_map_db.pkl`](pkl-db.md) (NC-v5 / **MAVERICK**). This page enumerates the
three host-IF / interconnect surfaces that the address DB describes: the **PCIe endpoint
apertures** (`USER_PCIEA` / `SECURE_PCIEM` views), the **UCIE die-to-die links**
(UCIE-A/-S × EW/NS, each a TL + LL + 2nm PHY + LTSM stack), the **d2d QSPI/PLL sideband**,
and the **on-chip TILE/IO/PEB/SP fabrics + HBM crossbar NoC** — then reconciles the
cross-die routing to the [NEIGHBOR decoder](addr-decode.md) and tabulates the
Maverick-vs-Cayman generational delta.

> **⚠️ ARCH WALL — MAVERICK (NC-v5), interior INFERRED.** The shipped pkl/json describe
> the **Maverick** SoC: every node binds a schema under `/proj/maverick/.../arch-regs/`.
> The DB **structure** (node tree, bases, sizes, schema bindings, counts) is byte-grounded
> and `[OBSERVED]`. The **v5 microarchitectural interior** (why EW vs NS, the exact
> cross-die wiring, the A-vs-S link semantics beyond the PHY macro) is `[INFERRED]`. The
> [Cayman host BAR layout](pcie-bars.md) + the flat Cayman YAML are byte-grounded
> **cross-gen** reference (NC-v3), not the same silicon. Every cross-gen difference is
> flagged in [§9](#9-maverick-vs-cayman-delta--the-headline-generation-change).

All facts below are streamed from the pkl/json — never `pickle.load`'d. Counts are
machine-derived from the
[`RestrictedUnpickler`](pkl-db.md#recipe-b--guarded-unpickler-defense-in-depth-optional) materialization
(`len == 323,198`, no global ever resolved) and cross-checked against the `.json` mirror.
Claims that depart from the page default `HIGH · OBSERVED` carry an explicit tag.

---

## 0. Where this subtree lives in the 323,198-record DB

The [pkl-db page](pkl-db.md) established the DB invariants this page reuses verbatim:
record total **323,198**, the **23-field** node schema, and **5 access-domain views**
keyed by address bits `[63:60]`:

| view (`parent_names[1]`) | nibble `[63:60]` | base | this page |
|--------------------------|:----------------:|------|-----------|
| `USER_INT`     | `0x0` | `0x0000000000000000` | FIS_UCIE protection mirrors only |
| **`USER_PCIEA`**   | `0x1` | `0x1000000000000000` | **PCIe data-plane apertures ([§2](#2-the-pcie-host-interface-plane--two-endpoint-aperture-views-high--observed))** |
| `SECURE_INT`   | `0x2` | `0x2000000000000000` | **all real UCIE/D2D/fabric CSRs (PEB plane)** |
| **`SECURE_PCIEM`**  | `0x3` | `0x3000000000000000` | **PCIe mgmt-plane apertures ([§2](#2-the-pcie-host-interface-plane--two-endpoint-aperture-views-high--observed))** |
| `USER_POD`     | `0x4` | `0x4000000000000000` | — |

The **PCIe views are `USER_PCIEA` (0x1) + `SECURE_PCIEM` (0x3)** — the USER/SECURE × PCIe
half of the access-domain matrix. The **UCIE/D2D/fabric controller register files** live
**only** in `SECURE_INT`, on the **PEB plane** (the `PEB_APB_IO` + `PEB_APB_IO_BCAST` dual
aperture reached via the bit-53 PEB flag); `USER_INT` carries only the host-visible
`FIS_UCIE` protection wrappers, no real UCIE controller.

```text
ADDRESS_MAP (root)
├── USER_PCIEA   [63:60]=0x1 ─ PCIEA_0..7  (BAR_OFFSET + MSIX_DOORBELL)     ── §2
├── SECURE_PCIEM [63:60]=0x3 ─ PCIEM_0..7  (BAR_OFFSET + MSIX_DOORBELL)     ── §2
├── USER_INT     [63:60]=0x0 ─ …SENG_n.{c,h}_die.APB_IO.FIS_UCIE_*  (mirror)
└── SECURE_INT   [63:60]=0x2 ─ …SENG_n.{c,h,io}_die.PEB_APB_IO[_BCAST]
                                 ├── AMZN_UCIE_{A,S}_{EW,NS}_n  (ucie_*_wrap)  ── §3,§4
                                 ├── AMZN_PEB.{D2D_QSPIM,D2D_QSPIS,PLL_D2D}    ── §4d
                                 ├── AMZN_TILE_FABRIC_n (URB + HBM_XBAR_4X2)   ── §6
                                 ├── AMZN_PEB_SP_FABRIC_n / FABRIC / AMZN_IO_FABRIC
                                 └── HBM_XBAR_8X32 / xbar_port_{hbm,fab}_{rd,wr} ── §6b
```

---

## 1. Executive summary

* **Three host-IF / interconnect surfaces** in this DB:
  1. **PCIe host-interface** — the two dedicated PCIe views. Tiny: **50 records** total
     (24 + 24 + 2 view nodes). These are **address apertures** (per-endpoint BAR window +
     MSI-X doorbell window), **not** controller CSRs.
  2. **d2d (die-to-die) = UCIE** — the inter-die chiplet interconnect, in the secure PEB
     plane. **UCIE 53,720** records, **D2D 33,432** records (D2D ∩ UCIE = 33,000). **39
     distinct UCIE links** (UCIE-A EW 18 / UCIE-A NS 8 / UCIE-S EW 8 / UCIE-S NS 5). Each
     link = a UCIE wrapper = `D2D_TL` (transaction layer, credit-based) + `D2D_LL` (link
     layer, RX/TX channels) + a 2nm UCIE PHY + LTSM (link-training state machine).
  3. **on-chip fabric / NoC** — `TILE_FABRIC` (16,624) + `PEB_FABRIC` (1,932) +
     `SP_FABRIC` (1,512) + `IO_FABRIC` (428) + HBM crossbars (`XBAR` 4,728). The tile
     fabric carries `READ_URB_0..8` / `WRITE_URB_0..8` buffers + an embedded
     `HBM_XBAR_4X2` — the switch between engines, HBM, and the host IF.

* **The PCIe/UCIE *controller* register files are NOT in this DB.** No `pcie5` /
  `snps_ctrl` / `dwc_pcie` / `mrvl` / `xsr` / `e32mp` / `appaxi` node is bound by `json` or
  named anywhere in the 323,198 records (**0 hits** for each; the only `dwc`
  substring hit is `DWC_HBMPHY` (16, the HBM PHY), the only `ring` hits are `ring_io_*`/`otp`,
  and the `bridge` hits are the per-link `s2s_bridge_ch_split`). The host PCIe controller is
  **real** (the `intc_pcie` subsystem carries 125 interrupt causes incl. FLR/SBR/link-down/
  SerDes-complex/BAR-hit), but its CSRs are a **separate generator scope** — see
  [hbm-d2d-pcie-blocks](../csr/hbm-d2d-pcie-blocks.md). This DB enumerates only the **endpoint
  apertures + MSI-X doorbells**.

* **Maverick-vs-Cayman headline:** Cayman built d2d on a **Synopsys DWC PCIe controller
  repurposed die-to-die + Marvell XSR SerDes + iATU + PCIe LTSSM**; Maverick switched to a
  **native UCIE 2nm chiplet link** — a proper TL/LL/PHY UCIE stack with credit-based flow
  control + LTSM. The IP family changed from "PCIe-SerDes-as-d2d" to "native UCIE". The HBM
  BAR4 aperture also **doubled**: Maverick 4 × 128 GiB = **512 GiB** vs Cayman 4 × 64 GiB =
  256 GiB.

* **Record arithmetic closes exactly** ([§7](#7-record-arithmetic-closure-high--observed)): PCIE
  50 = 24 + 24 + 2; UCIE 53,720 = 40,232 (link subtrees) + 8,340 (FIS protection) + 5,148
  (tile traffic-gen); D2D 33,432 = 33,000 (in-UCIE TL/LL/PHY) + 432 (QSPI/PLL/config
  sideband). The `.json` mirror counts are byte-identical (PCIE 50 / UCIE 53,720 / D2D
  33,432).

---

## 2. The PCIe host-interface plane — two endpoint-aperture views `[HIGH · OBSERVED]`

PCIe in this DB is **not** under SENG/APB_IO; it is the two dedicated top-level views.
Each view node is `type=NODE`, holds **8 endpoint NODES**, and each endpoint holds **2
INDIRECT children**. **50 PCIE-keyword records whole-DB** (streamed; `.json` mirror = 50).

### 2a. `USER_PCIEA` view — the user / application data-plane PCIe space

Each `PCIEA_n` is `count=1`, `self_array_size="8"`, `instance_index 0x0..0x7`,
`parent_names=[ADDRESS_MAP, user_pciea]`, `json = .../address_map/pciea_address_map.json`,
`type=NODE`. Bases stride by exactly the size (`0x200000000000000` = 128 PiB, back-to-back):

| short_name | base | size | size (human) | json |
|------------|------|------|--------------|------|
| `PCIEA_0` | `0x1000000000000000` | `0x200000000000000` | 128 PiB | `pciea_address_map.json` |
| `PCIEA_1` | `0x1200000000000000` | `0x200000000000000` | 128 PiB | `pciea_address_map.json` |
| `PCIEA_2` | `0x1400000000000000` | `0x200000000000000` | 128 PiB | `pciea_address_map.json` |
| `PCIEA_3` | `0x1600000000000000` | `0x200000000000000` | 128 PiB | `pciea_address_map.json` |
| `PCIEA_4` | `0x1800000000000000` | `0x200000000000000` | 128 PiB | `pciea_address_map.json` |
| `PCIEA_5` | `0x1a00000000000000` | `0x200000000000000` | 128 PiB | `pciea_address_map.json` |
| `PCIEA_6` | `0x1c00000000000000` | `0x200000000000000` | 128 PiB | `pciea_address_map.json` |
| `PCIEA_7` | `0x1e00000000000000` | `0x200000000000000` | 128 PiB | `pciea_address_map.json` |

Each endpoint's **2 INDIRECT children** (both `type=NODE`, both bind `reserved.json` — they
are address **windows**, not CSR leaves):

| child (`short_name`) | offset | size | size (human) | role |
|----------------------|:------:|------|:------------:|------|
| `_BAR_OFFSET`     | `+0x0`               | `0x2000000000000` | 512 TiB | the BAR aperture base (where the host BAR targets the SoC) |
| `_MSIX_DOORBELL`  | `+0x2000000000000`  | `0x2000000000000` | 512 TiB | the MSI-X doorbell write window (sits at **bit 49** of the endpoint span) |

So e.g. `USER_PCIEA_PCIEA_0_BAR_OFFSET` @ `0x1000000000000000` and
`USER_PCIEA_PCIEA_0_MSIX_DOORBELL` @ `0x1002000000000000` (Δ = `0x2000000000000`,
bit 49 set).

### 2b. `SECURE_PCIEM` view — the secure / management PCIe space

`PCIEM_0` @ `0x3000000000000000` … `PCIEM_7` @ `0x3e00000000000000`, **byte-identical
structure** (same size/stride/children), binding `pciem_address_map.json`.

### 2c. PCIEA vs PCIEM identity `[HIGH structural · OBSERVED; MED label · INFERRED]`

Two PCIe access functions distinguished only by the top address nibble: `USER_PCIEA`
(0x1) = the USER / Application data-plane function; `SECURE_PCIEM` (0x3) = the SECURE /
Management function. 8 endpoints per function. The bases + counts + structure are
`[HIGH · OBSERVED]`; the **A = Application / M = Management** naming is name-inferred
`[MED · INFERRED]`.

### 2d. The on-disk schema binding

`pciea_address_map.json` is a `RegFile` with `UnitName "PCIEA_ADDRESS_MAP"`, `Type NODE`,
`InterfaceType APB`, `AddrWidth 64`, `SizeInBytes 0x200000000000000`, and **two `INCL_TYPE
INDIRECT` includes** both binding `address_map/reserved.json`:

* `msix_doorbell` @ `AddressOffset 0x2000000000000`, `RESERVED_SIZE 0x2000000000000`
* `bar_offset`    @ `AddressOffset 0x0`,              `RESERVED_SIZE 0x2000000000000`

`pciem_address_map.json` is identical. The pkl PCIe-view nodes are **byte-for-byte** the
on-disk schemas.

### 2e. Reconciliation vs the host BAR layout

> **DISTINCTION (CORRECTION-grade).** [pcie-bars.md](pcie-bars.md) documents the **host-driver
> BAR view** — BAR0 (control) + BAR4 (HBM) + the PEB amzn-function BARs: *where the host pokes
> the chip*. **This** view is the **SoC-side endpoint address apertures** — *the chip's own
> per-function PCIe address windows + MSI-X doorbells*. They are **different objects**. The
> MSIX_DOORBELL + BAR_OFFSET windows here are the SoC-side **targets** of the host BAR's
> inbound translation; the iATU region registers that perform that translation live in the PCIe
> controller (`PF0_ATU_CAP`), which this address DB does **not** enumerate
> (0 `iatu`/`atu` nodes).

The Maverick **host** BAR layout is in the shipped `maverick_pcie_bar_mapping.yaml` /
`pcie_bar_defines.h`: **BAR0** organized per-SENG (4 SENG ×
`{APB_IO 512 MiB, TPB_n_SBUF 64 MiB, TPB_n 32 MiB, TPB_n_LOCAL_SUNDA_POOL_RSVD 32 MiB,
H_DIE_SCRATCHPAD 32 MiB}`, top byte `0xc8000000`); **BAR4** = 4 × `0x2000000000`
(128 GiB) = **512 GiB**, packed (stride == size). The Maverick BAR0 is per-SENG vs Cayman's
flat `APB_IO/APB_SE/PREPROC/TPB/TOP_SP/RDM/INTC` window set; BAR4 HBM is doubled.

---

## 3. The d2d (die-to-die) plane = UCIE — link controller layout

The Maverick die-to-die interconnect is **UCIE** (Universal Chiplet Interconnect Express).
A d2d link = an `AMZN_UCIE_{A|S}_{EW|NS}_n` NODE (`json = ucie_a_wrap.json` or
`ucie_s_wrap.json`, `size 0x200000` = 2 MiB), living **only** in `SECURE_INT` on the PEB
plane. Canonical sample:

```text
SECURE_INT_SENG_0_C_DIE_PEB_APB_IO_AMZN_UCIE_A_EW_0
   base=0x2000008013180000  size=0x200000  type=NODE  json=ucie_a_wrap.json
```

The two sub-wrappers and their contents, **byte-exact** (offsets shown relative to their
**immediate parent**, to avoid the link-base vs wrapper-base ambiguity):

| parent | block | type | size | json (schema) | role |
|--------|-------|------|------|---------------|------|
| **link** | `D2D_TL_WRAPPER` @ `+0x000000` | REGFILE | `0x080000` | `d2d_tl_wrapper.json` | **UCIE TRANSACTION LAYER** |
| TL_WRAPPER | `FIS_0` @ `+0x00000` | NODE | `0x10000` | `fis_type_1000_amzn.json` | fabric-interface slice (+`fis_control`) |
| TL_WRAPPER | `TL` @ `+0x10000` | REGFILE | `0x02000` | `d2d_tl.json` | TL credit/flow regs ([§5](#5-the-ucie-transaction-layer-d2d_tljson--credit-based-chiplet-protocol-high--observed)) |
| TL_WRAPPER | `CH_SPLITTER` @ `+0x20000` | REGFILE | `0x00100` | `s2s_bridge_ch_split.json` | S2S-bridge channel splitter |
| TL_WRAPPER | `TL_INTC` @ `+0x40000` | INTC | `0x00100` | `d2d_tl_intc.json` | TL interrupt ctrl (4-grp) |
| **link** | `D2D_LL_PHY_WRAPPER` @ `+0x100000` | NODE | `0x100000` | `d2d_ll_phy_wrapper.json` | **UCIE LINK LAYER + PHY** |
| LL_PHY_WRAPPER | `FIS_0` @ `+0x00000` | NODE | `0x10000` | `fis_type_1000_amzn.json` | fabric-interface slice |
| LL_PHY_WRAPPER | `LL` @ `+0x10000` | REGFILE | `0x02000` | `d2d_ll.json` | link-layer regs |
| LL.LL | `RX_CH` @ `+0x00800` | REGFILE | `0x00800` | `d2d_ll_rx.json` | LL receive channel (+ `RAS_RECORD` = `ap_arm_ras`) |
| LL.LL | `TX_CH` @ `+0x01800` | REGFILE | `0x00800` | `d2d_ll_tx.json` | LL transmit channel (+ `RAS_RECORD`) |
| LL_PHY_WRAPPER | `ELA` @ `+0x12000` | REGFILE | `0x01000` | `cxela500.json` | ARM ELA embedded debug |
| LL_PHY_WRAPPER | `ERG` @ `+0x14000` | REGFILE | `0x00040` | `erg_ecc_model.json` | ECC engine |
| LL_PHY_WRAPPER | `PHY_WRAPPER` @ `+0x15000` | REGFILE | `0x01000` | `d2d_phy_wrapper_syn.json` | PHY wrapper (+`BIST`, +`LTSM_LOG_FIFO`) |
| PHY_WRAPPER | `BIST` @ `+0x00800` | REGFILE | `0x00200` | `d2d_phy_bist.json` | PHY built-in self-test |
| PHY_WRAPPER | `LTSM_LOG_FIFO` @ `+0x00a00` | TABLE | `0x00040` | `d2d_phy_wrapper_syn_ltsm_log_fifo.json` | **Link-Training SM log FIFO** |
| LL_PHY_WRAPPER | `LL_PHY_INTC` @ `+0x16000` | INTC | `0x00100` | `d2d_ll_phy_intc.json` | LL/PHY interrupt ctrl |
| LL_PHY_WRAPPER | `PHY` @ `+0x20000` | REGFILE | `0x10000` | `d2c_ucie2phy_top_a_2nm_ew.json` | **the 2nm UCIE PHY** |

All 11+ schema JSONs are present on disk: `ucie_a_wrap NODE 0x200000`,
`d2d_tl_wrapper REGFILE 0x80000`, `d2d_ll_phy_wrapper NODE 0x100000`, `d2d_tl/d2d_ll REGFILE
0x2000`, `d2d_phy_wrapper_syn 0x1000`, `d2c_ucie2phy_top_a_2nm_ew 0x10000`,
`s2s_bridge_ch_split 0x100` — node `type`/`size` == schema `Type`/`SizeInBytes`.

> **GOTCHA — wrapper-relative offsets.** An "off in link" column that mixes
> link-base and wrapper-base offsets is ambiguous: `ELA +0x12000` and `PHY +0x20000`
> are relative to `D2D_LL_PHY_WRAPPER`, **not** the link base. Absolute address of the PHY
> = `link_base + 0x100000 (LL_PHY_WRAPPER) + 0x20000` = `link_base + 0x120000`. This page
> states every offset relative to its **immediate parent** (the `offset` field) so
> the address arithmetic is unambiguous.

### 3a. UCIE-A vs UCIE-S — only the PHY macro differs

`AMZN_UCIE_S_EW_0` (`ucie_s_wrap.json`) is **byte-identical** in structure; the only
difference at the address-map level is the PHY: it binds `d2c_ucie2phy_top_s_2nm_ew.json`
(the S-flavor 2nm UCIE PHY, same `offset 0x20000`, same `size 0x10000`). Whether firmware
treats UCIE-A vs UCIE-S as physically distinct link classes or just two PHY flavors of one
fabric is a firmware question `[LOW · INFERRED]`; the address map shows two wrapper schemas
differing only in the PHY macro.

### 3b. PHY-name decode + LTSM `[HIGH · OBSERVED name; MED · INFERRED semantics]`

`d2c_ucie2phy_top_{a|s}_2nm_{ew}` = "die-to-chiplet UCIE-to-PHY top, flavor {A|S}, **2nm**
process, **East-West** direction". The `PHY_WRAPPER` carries the **LTSM** (Link-Training
State Machine) log FIFO + `d2d_phy_bist` — the UCIE link-bring-up + at-speed test surface
(the UCIE analog of PCIe's LTSSM). Each LL RX/TX channel carries an `ap_arm_ras` RAS record;
each link has an `ERG` (ECC) + `cxela500` ELA debug block + `regfile_{security,access,parity}_log`
RAS instrumentation in every `GLOBALS` sub-bundle — mirroring the HBM-datapath RAS surface.

---

## 4. UCIE link geometry + cross-die routing `[HIGH · OBSERVED geometry; MED · INFERRED routing]`

### 4a. Link count per package — **39 distinct links**

Matched by `short_name == AMZN_UCIE_{A|S}_{EW|NS}_n` where `json` binds
`ucie_{a,s}_wrap.json`:

| flavor | dir | distinct link idx | on which die(s) |
|--------|:---:|-------------------|-----------------|
| UCIE-A | EW | **18** (idx 0..17) | C_DIE has 0..15 (16); H_DIE has 0..17 (18, incl 2 H_DIE-only EW_16/17) |
| UCIE-A | NS | **8** (idx 0..7)  | C_DIE only |
| UCIE-S | EW | **8** (idx 0..7)  | C_DIE only |
| UCIE-S | NS | **5** (idx 0..4)  | C_DIE only |
| | | **TOTAL 39** | |

Per-die: **C_DIE = 37 links** (16 A-EW + 8 A-NS + 8 S-EW + 5 S-NS); **H_DIE = 18 A-EW**
(the 16 shared EW + 2 H_DIE-only EW_16/17). Top-container records: **440** (by die:
C_DIE 296 + H_DIE 144).

### 4b. Instance multiplicity — decoding the 440 top-containers

Each link top-container is replicated across `{4 SENG} × {dies it lives on} × {PEB_APB_IO
plain, PEB_APB_IO_BCAST}`:

| link set | replication | instances each | top-container total |
|----------|-------------|:--------------:|:-------------------:|
| A-EW_0..15 | 4 SENG × 2 dies (C+H) × 2 apertures | 16 (`sas="16"`) | 16 × 16 = 256 |
| A-EW_16,17 | 4 SENG × 1 die (H only) × 2 apertures | 8 (`sas="18"`) | 2 × 8 = 16 |
| A-NS_*, S-EW_*, S-NS_* | 4 SENG × 1 die (C only) × 2 apertures | 8 | (8+8+5) × 8 = 168 |
| | | **TOTAL** | **440** |

The `PEB_APB_IO_BCAST` aperture is **+0x20000000** (bit 29) from `PEB_APB_IO` — on a
link pair: plain `AMZN_UCIE_A_EW_0` @ `0x2000008013180000`, bcast @ `0x2000008033180000`,
Δ = `0x20000000`. This is the same dual-aperture broadcast delta the HBM and DMA subtrees
use ([rdma-cross-die](../../dma/rdma-cross-die.md)).

### 4c. Cross-die routing — reconciliation vs the NEIGHBOR decoder `[HIGH geometry · OBSERVED; MED route · INFERRED]`

The [NEIGHBOR decoder](addr-decode.md#3-neighbor-decoder--cayman_addr_decode_neighbor) splits the
address top bits `[53:48]` into per-direction routing flags that replace the LOCAL view's
`CAYMAN_ID`:

| flag | bit | extractor (from addr-decode) | meaning |
|------|:---:|------------------------------|---------|
| `EXIT_SENG`      | `[50]` | `(addr>>50)&0x1` | leave the local Sequencer-Engine domain |
| `EXIT_DIE`       | `[51]` | `(addr>>51)&0x1` | cross the originating-die boundary |
| `NEIGHBOR_ROUTE` | `[52]` | `(addr>>52)&0x1` | engage the neighbor-routing path |
| `PEB`            | `[53]` | `(addr>>53)&0x1` | the bit-53 PEB aperture (where the UCIE CSRs live) |

The UCIE geometry here is the **physical fabric those flags steer onto**:

* **EW (East-West) links present on BOTH C_DIE and H_DIE** (A-EW 0..15) → the
  chiplet-to-chiplet **mesh edges**, the `NEIGHBOR_ROUTE`/`CAYMAN_ID` cross-chip path: each
  die has its own east + west UCIE ports into the package mesh.
* **NS (North-South) links C_DIE-only** (A-NS, S-NS) → the local **inter-die** connection
  (the `EXIT_DIE` axis: C_DIE reaching H_DIE/IO_DIE within the package). The compute die
  (C_DIE) is the routing hub that owns the NS links; H_DIE needs only EW.
* The **2 H_DIE-only EW links** (A-EW_16,17) → extra HBM-die east-west ports.

The decode **headers** define the *address envelope*; **this DB** enumerates the *UCIE link
hardware* that envelope's routing bits select. The link existence + EW/NS/die placement is
`[HIGH · OBSERVED]`; the **EW = cross-chip-mesh / NS = inter-die** reading is
name+topology `[MED · INFERRED]` — not a wiring trace. The local routing stage that
demultiplexes a transaction onto the UCIE virtual channels is the `s2s_bridge_ch_split`
(CH_SPLITTER, one per link) — the in-DB analog of the addr-decode `io_d2d` / `user_remapper`
CAM ("the CAM that consumes `CAYMAN_ID`").

> **NOTE — the `CAYMAN_ID` field-name carries across SoCs.** The NEIGHBOR decoder header is
> a **Cayman** artifact (`cayman_addr_decode_neighbor.h`), but the field layout
> (`EXIT_SENG[50]`/`EXIT_DIE[51]`/`NEIGHBOR_ROUTE[52]`/`PEB[53]` over a 6-bit chip-id mesh)
> is the family-general cross-die envelope. Maverick reuses the same `[63:60]` view-nibble +
> bit-53 PEB scheme (every PCIe view, every UCIE CSR, every fabric node lands
> on the predicted nibble/plane). The Maverick die set is **3-die** (C_DIE/H_DIE/IO_DIE) vs
> Cayman's 2-die `DIE[47]` — that is `[INFERRED]` for the v5 interior wiring.

### 4d. The d2d sideband — QSPI + PLL config plane

**432 D2D-only records** (not inside any UCIE link) = the per-die d2d clock+config sideband,
under `AMZN_PEB`, on **all three dies** (C_DIE/H_DIE/IO_DIE), **144 records each**:

| block | inst | size | json | role |
|-------|:----:|------|------|------|
| `D2D_QSPIM` | 24 | `0x1000` | `d2d_config.json` | QSPI **master** (UCIE sideband cfg) |
| `D2D_QSPIS` | 24 | `0x1000` | `spi_slave_wrapper.json` | QSPI **slave** (`SPIS_AXI_CFG`/`PASW`) |
| `PLL_D2D`   | 24 | `0x80000` (512 KiB) | `pll_amzn.json` | the dedicated **d2d-link clock PLL** |

(24 each = 4 SENG × 3 dies × 2 apertures.) The QSPI master/slave is the UCIE sideband
configuration/flash channel (chiplet management); `PLL_D2D` is the d2d-link clock. The
sideband lives on **IO_DIE too** (which carries **no** UCIE link wrapper — only the
clock+config). The QSPI = sideband-config reading is name-inference `[MED · INFERRED]`.

---

## 5. The UCIE transaction layer (`d2d_tl.json`) — credit-based chiplet protocol `[HIGH · OBSERVED]`

`d2d_tl.json` (`UnitName "d2d_tl"`, `Desc "D2D Transport Layer registers"`, REGFILE `0x2000`)
register surface, read from the on-disk schema:

| bundle / reg | role |
|--------------|------|
| `sec_ovrd` | security override |
| `num_{wdat,rdat,rreq,wreq,cmp}_credits` | per-channel credit counts (5 channel classes) |
| `{wdat,rdat,rreq,wreq,cmp}_credit_return_num` | per-channel credit-release amounts |
| `recal_flow_en`, `recal`, `recal_link_st`, `recal_ack`, `recal_fsm_st`, `sw/hw_recal_link_dis` | recalibration flow ("Set is done by LL") |
| `{rreq,wreq,wdat,rdat}_int_clr`, `*_crd_ovf_int_clr` | interrupt / credit-overflow clears |
| `rate_limit` → `rate` | a **token-bucket** valid-rate limiter to the link ("Tokens grant rate") |
| `unit`, `version`, `status` | TL info / versioning |

So the UCIE transaction layer is a **credit-based, virtual-channel flow-controlled
transport** with 5 channel classes — `{write-req, read-req, write-data, read-data,
completion}` = `{wreq, rreq, wdat, rdat, cmp}` — a token-bucket valid-rate limiter, TL
versioning, a recalibration handshake (TL ⇄ LL), and a security override. `s2s_bridge_ch_split.json`
(`CH_SPLITTER`, REGFILE `0x100`) is the S2S-bridge channel splitter that demultiplexes the SoC
AXI/protocol stream onto the TL virtual channels — the local routing stage before the UCIE link
`[HIGH name · OBSERVED; MED routing-role · INFERRED]`.

These TL credit channels are the **hardware substrate** that the inter-die RDMA / collective
transport rides on (the runtime *use* — how RDMA descriptors traverse the link — is the
domain of [rdma-cross-die](../../dma/rdma-cross-die.md), cited here, not re-derived).

---

## 6. The on-chip fabric / NoC subtree

The on-chip interconnect between host-IF, HBM, and the engines is a multi-fabric set, all in
the secure PEB plane (+ host-visible FIS_* mirrors). Container nodes (re-streamed by `json`
binding):

| fabric container (`json`) | nodes | short_name(s) | role |
|---------------------------|:-----:|---------------|------|
| `tile_fabric_amzn.json` (0x80000) | 64 | `AMZN_TILE_FABRIC_0..7` (8 inst each) | tile↔HBM NoC ([§6a](#6a-amzn_tile_fabric--urb--embedded-crossbar-high--observed)) |
| `top_sp_fabric_amzn.json` | 48 | `AMZN_PEB_SP_FABRIC_0/1` (24 each) | TOP_SP-side fabric |
| `peb_fabric_amzn.json` | 24 | `FABRIC` | PEB fabric |
| `io_fabric_amzn.json` | 8 | `AMZN_IO_FABRIC` | IO-side fabric |

Whole-DB keyword totals (streamed; user/secure split via the dual aperture):
`TILE_FABRIC` **16,624** / `PEB_FABRIC` **1,932** / `SP_FABRIC` **1,512** / `IO_FABRIC`
**428** / `XBAR` **4,728**. Host-visible FIS mirrors (`FIS_TILE_FABRIC`, `FIS_PEB_SP_FABRIC`,
`FIS_PEB_FABRIC`, `FIS_IO_FABRIC`) carry the user-view protection wrappers.

### 6a. `AMZN_TILE_FABRIC` — URB + embedded crossbar `[HIGH · OBSERVED]`

`AMZN_TILE_FABRIC_0` (`tile_fabric_amzn.json`, `0x80000` = 512 KiB) direct children:

| child | type | size | json | count |
|-------|------|------|------|:-----:|
| `FIS_0` | NODE | `0x10000` | `fis_type_1000_amzn.json` | 1 |
| `READ_URB_0..8` | REGFILE | `0x400` ea | `tile_fabric_urb.json` | **9** |
| `WRITE_URB_0..8` | REGFILE | `0x400` ea | `tile_fabric_urb.json` | **9** |
| `HBM_XBAR_4X2` | NODE | `0x20000` | `hbm_xbar_4x2_amzn.json` | 1 |

The tile fabric is a **9-read / 9-write URB** (Unified Request Buffer) interconnect with an
embedded **`HBM_XBAR_4X2`** crossbar — the NoC stage that routes tile-engine traffic into the
HBM crossbar plane.

### 6b. The HBM crossbar NoC

`XBAR` = 4,728 records whole-DB. Crossbar schema census (`json` tally):

| schema | count | role |
|--------|:-----:|------|
| `xbar_port_hbm_rd.json` / `xbar_port_hbm_wr.json` | 640 / 640 | per-port HBM read / write ports |
| `xbar_port_fab_rd.json` / `xbar_port_fab_wr.json` | 384 / 384 | per-port fabric read / write ports |
| `hbm_xbar_8x32_{amzn,user}.json` | 16 / 24 | the **8×32** secure/user crossbar |
| `hbm_xbar_4x2_{amzn,user}.json` | 64 / 96 | the **4×2** tile-fabric crossbar |
| `hbm_xbar_8x32_fis_user.json` | 192 | crossbar FIS protection |
| `xbar_central_ctrl.json` | 160 | switch controller |
| `xbar_user.json` / `xbar_amzn.json` | 120 / 80 | user / secure xbar regs |
| `xbar_crc_hash.json` | 120 | address-hash / channel-interleave remapper |

`HBM_XBAR_8X32` (8 client ports × 32 HBM datapaths, secure plane) + `HBM_XBAR_4X2` (4×2,
tile/user plane) **are** this NoC: `xbar_port_hbm_*` (640) the HBM-side ports,
`xbar_port_fab_*` (384) the fabric-side ports, `xbar_crc_hash` the channel-interleave
remapper, `xbar_central_ctrl` the switch controller. **No node carries `NOC`, `RING`
(other than `ring_io_*`), or a dedicated bridge name** (0 `NOC` hits) — the
Maverick on-chip interconnect is expressed as the FABRIC (tile/io/peb/sp) + XBAR families,
not a named NoC/ring.

---

## 7. Record-arithmetic closure `[HIGH · OBSERVED]`

Closes to the byte; cross-checked against the `.json` mirror.

### 7a. PCIe domain — 50 (mirror = 50)

```text
USER_PCIEA  view : 24 = 8 endpoints × 3 records (PCIEA_n + _BAR_OFFSET + _MSIX_DOORBELL)
SECURE_PCIEM view: 24 = 8 endpoints × 3 records
+ 2 view nodes (USER_PCIEA, SECURE_PCIEM)
────────────────────────────────────────────
PCIE total = 24 + 24 + 2 = 50   ✓ EXACT
```

### 7b. UCIE — 53,720 (mirror = 53,720)

```text
UCIE link-wrapper subtrees   40,232   (440 link top-containers; mix of 61- and 154-record links)
FIS_UCIE protection wrappers  8,340   (user/host + secure/priv, priv = 2× host)
UCIE_TILE_TG traffic-gen      5,148
────────────────────────────────────────────
UCIE total                   53,720   ✓ EXACT
```

### 7c. D2D — 33,432 (mirror = 33,432)

```text
D2D blocks INSIDE UCIE links (D2D_TL_WRAPPER / D2D_LL_PHY_WRAPPER + descendants)  33,000
D2D SIDEBAND not in a UCIE link (D2D_QSPIM/QSPIS/PLL_D2D + GLOBS/CTL/violations)     432
────────────────────────────────────────────
D2D total                                                                        33,432   ✓ EXACT
```

`D2D ∩ UCIE = 33,000` — the d2d-named protocol blocks live **inside** UCIE
link wrappers; D2D-only = 432 = the `AMZN_PEB` sideband.

### 7d. Fabric / NoC keyword totals (clean censuses, not disjoint)

`TILE_FABRIC` 16,624 / `PEB_FABRIC` 1,932 / `SP_FABRIC` 1,512 / `IO_FABRIC` 428 / `XBAR`
4,728. These overlap the PEB / FIS families and are not disjoint partitions, so no single
grand sum is asserted — each is a clean keyword census.

---

## 8. JSON-sibling equivalence

The `.json` mirror was streamed as text (`rg`/`jq`, no pickle risk). Keyword counts are
byte-identical to the pkl: **PCIE 50 == 50; UCIE 53,720 == 53,720; D2D 33,432 == 33,432;
total 323,198 == 323,198**. Representative record `USER_PCIEA_PCIEA_0`: pkl
`base=0x1000000000000000 size=0x200000000000000 type=NODE self_array_size="8"
json=…/pciea_address_map.json` == json `"base": 1152921504606846976, "size":
144115188075855872, "type": "NODE", "self_array_size": "8"` — base/size/type/offset all
byte-exact. On-disk schema `Type`/`SizeInBytes` == node `type`/`size` for every UCIE/PCIe
schema verified.

---

## 9. Maverick vs Cayman delta — the headline generation change

| aspect | CAYMAN (NC-v3, cross-gen ref) | MAVERICK (NC-v5, this pkl) | conf |
|--------|-------------------------------|----------------------------|:----:|
| d2d link IP | `snps_ctrl` = DWC PCIe ctrl **repurposed** d2d (`PF0_PCIE_CAP`/AER/ATU/PTM/LTSSM) | **native UCIE**: `ucie_a/s_wrap` = `d2d_tl` + `d2d_ll` + 2nm PHY | HIGH |
| d2d PHY | Marvell XSR SerDes + Marvell MPCS x16 (`mrvl_xsr_phy`) | `d2c_ucie2phy_top_{a,s}_2nm_ew` (UCIE 2nm PHY) + BIST | HIGH |
| d2d link train | PCIe LTSSM (`SD_STATUS_L1LTSSM_REG`) | UCIE **LTSM** (`ltsm_log_fifo`) | HIGH |
| d2d addr translation | iATU `PF0_ATU_CAP` outbound regions | `s2s_bridge_ch_split` + TL virtual-channel credits | HIGH (blocks) \| MED (routing) |
| d2d transport | PCIe TLP (req/cmpl, AER) | UCIE TL credit-based `{wreq,rreq,wdat,rdat,cmp}` | HIGH |
| d2d links/die | 8 D2D subsystems/die | **37 (C_DIE) / 18 (H_DIE)**; 39 distinct (A-EW18, A-NS8, S-EW8, S-NS5) | HIGH |
| d2d sideband | (not enumerated as addr-map nodes) | `D2D_QSPIM`/`QSPIS` + `PLL_D2D` per die (C/H/IO) | HIGH |
| PCIe host ctrl | `pcie5_x8_DWC_pcie_ctl` (MSI/MSI-X/PL16G/PL32G) + e32mp PHY | **NOT in this addr-DB** (0 nodes; controller in `intc_pcie` scope) | HIGH |
| PCIe in addr-map | host BAR0/BAR4 windows | endpoint apertures: `USER_PCIEA` (0x1) + `SECURE_PCIEM` (0x3), 8 endpoints ea, +MSIX doorbell | HIGH |
| PCIe MSI-X | `PF0_MSIX_CAP` in pcie5 ctrl | `MSIX_DOORBELL` aperture/endpoint | HIGH |
| HBM BAR4 / stack | 4 × 64 GiB = **256 GiB** | 4 × 128 GiB = **512 GiB** | HIGH |
| host BAR0 layout | `APB_IO/APB_SE/PREPROC/TPB/TOP_SP/RDM/INTC` windows | per-SENG (4 × `{APB_IO, 2×TPB, scratchpad}`) | HIGH |
| on-chip fabric | `io_d2d` / xbar | TILE/IO/PEB/SP fabric + URB + `HBM_XBAR_8X32/4X2` + xbar ports | HIGH |
| die structure | 2-die (`DIE[47]`); 64-die `CAYMAN_ID` | 3-die (C_DIE/H_DIE/IO_DIE) | HIGH (count) \| INFERRED (wiring) |
| priv aperture | bit-53 `PEB_APB_IO` | `PEB_APB_IO` + `PEB_APB_IO_BCAST` (dual, +0x20000000) | HIGH |

> **The headline:** Cayman built die-to-die on a **DWC-PCIe controller repurposed as a d2d
> link** (Marvell XSR SerDes + iATU + LTSSM); Maverick switched to a **native UCIE (2nm)
> chiplet interconnect** — a proper TL/LL/PHY UCIE stack with credit-based flow control +
> LTSM. The host PCIe (data-plane) controller is unchanged in spirit (still the DWC-PCIe5
> family) but is **not** enumerated in this address DB — only its endpoint apertures +
> MSI-X doorbells are.

---

## 10. CORRECTION callouts + reconciliation

* **CORRECTION (offset basis).** A link sub-block table that mixes link-base and
  wrapper-base offsets is ambiguous. The `offset` fields show `ELA +0x12000` /
  `PHY +0x20000` etc. are **relative to `D2D_LL_PHY_WRAPPER`** (which itself sits at
  `+0x100000` in the link). The PHY absolute offset = `+0x120000`. This page states every
  offset relative to its immediate parent.

* **NOTE (controller-absence substrings).** A naive substring scan for d2d-controller IP
  produces false positives: `dwc`→`DWC_HBMPHY` (16, the HBM PHY), `ring`→`ring_io_*`/`otp`
  (72), `bridge`→`s2s_bridge_ch_split` (440, the legitimate per-link channel splitter). The
  **true** PCIe/UCIE *controller* node count is **0** (`pcie5`/`snps`/`mrvl`/`xsr`/`e32mp`/
  `appaxi`/`iatu`/`atu`/`noc` all = 0). The "no controller node" claim holds once
  the substring collisions are excluded.

* **Cross-checks consistent.** The d2d/UCIE + fabric CSRs sit on the same
  `PEB_APB_IO` (secure, bit-53, dual-aperture +0x20000000) control plane as the SDMA/HBM
  blocks — reached through the `SENG.{c,h}_die.PEB_APB_IO` chain with the same FIS/SPROT
  protection wrappers. The d2d **trigger** sources (link-down / RAS / FLR analogs) are in
  [pcie-hbm-tpb-d2d-triggers](../interrupt/pcie-hbm-tpb-d2d-triggers.md); the d2d/PCIe **CSR
  block** schemas are in [hbm-d2d-pcie-blocks](../csr/hbm-d2d-pcie-blocks.md).

---

## 11. Reimplementation pseudocode

### 11a. Carve the PCIe/D2D/fabric subtree (safe streaming, never `pickle.load`)

```python
"""
Carve the PCIe / UCIE / fabric subtree from al_address_map_db.pkl.

SAFETY: the pkl can execute code on load. NEVER pickle.load()/loads(). Either
(a) stream with pickletools.genops() for a pure opcode walk, or
(b) materialize with a RestrictedUnpickler whose find_class() RAISES on ANY
    global -- the pkl-db genops proof showed 0 dangerous opcodes, so the only
    constructors are list/dict/str/int and find_class is never legitimately hit.
Streaming the 514 MB .json mirror with ijson/jq is an equivalent safe path.
"""
import pickle, re

PKL = ".../arch-headers/maverick/ext/al_address_map_db.pkl"

class RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module, name):                  # any global => abort
        raise pickle.UnpicklingError(f"BLOCKED global {module}.{name}")

with open(PKL, "rb") as f:
    db = RestrictedUnpickler(f).load()                   # -> list, len == 323_198

def view_of(rec):                                        # parent_names[1] == access view
    pn = rec["parent_names"]
    return pn[1] if len(pn) > 1 else (pn[0] if pn else "")

def schema(rec):                                         # leaf node's bound schema file
    return rec["json"].rsplit("/", 1)[-1] if rec.get("json") else ""

# --- PCIe apertures: the two dedicated views ---
pcie  = [r for r in db if "PCIE" in r["name"]]            # 50 = 24 + 24 + 2 view nodes
pciea = [r for r in pcie if view_of(r) == "user_pciea"]   # 24, schema pciea_address_map.json
pciem = [r for r in pcie if view_of(r) == "secure_pciem"] # 24, schema pciem_address_map.json
# each endpoint PCIEA_n / PCIEM_n -> {_BAR_OFFSET @ +0x0, _MSIX_DOORBELL @ +0x2000000000000}

# --- UCIE links: top-container = wrapper node binding ucie_{a,s}_wrap.json ---
LINK = re.compile(r"AMZN_UCIE_([AS])_(EW|NS)_(\d+)$")
links = {}                                               # (flavor, dir, idx) -> [records]
for r in db:
    m = LINK.match(r["short_name"])
    if m and schema(r) in ("ucie_a_wrap.json", "ucie_s_wrap.json"):
        links.setdefault((m[1], m[2], int(m[3])), []).append(r)
assert len(links) == 39                                  # A-EW18, A-NS8, S-EW8, S-NS5

# --- d2d sideband: D2D-named records NOT inside any UCIE link ---
sideband = [r for r in db if "D2D" in r["name"] and "UCIE" not in r["name"]]
assert len(sideband) == 432                              # QSPIM/QSPIS/PLL_D2D × 3 dies × ...

# --- on-chip fabrics ---
fabrics = {jn: [r for r in db if schema(r) == jn] for jn in (
    "tile_fabric_amzn.json", "top_sp_fabric_amzn.json",
    "peb_fabric_amzn.json", "io_fabric_amzn.json")}      # 64 / 48 / 24 / 8

# --- record-arithmetic closure ---
assert len(pcie) == 50
assert sum(1 for r in db if "UCIE" in r["name"]) == 53_720
assert sum(1 for r in db if "D2D"  in r["name"]) == 33_432
```

### 11b. Map a UCIE link → a NEIGHBOR route

```python
"""
Given a UCIE link's address-map record, derive the NEIGHBOR-decoder egress
encoding it serves. The link's parent chain pins {view, SENG, die, PEB-plane};
the EW/NS axis + die placement select the NEIGHBOR routing flags.

addr-decode NEIGHBOR fields (cayman_addr_decode_neighbor.h):
  EXIT_SENG=[50], EXIT_DIE=[51], NEIGHBOR_ROUTE=[52], PEB=[53]   over CAYMAN_ID[53:48].
"""
def link_to_neighbor_route(rec):
    pn = rec["parent_names"]
    die  = next((p for p in pn if p in ("c_die", "h_die", "io_die")), "?")
    seng = next((p for p in pn if p.startswith("seng_")), "?")
    m = re.match(r"AMZN_UCIE_([AS])_(EW|NS)_(\d+)$", rec["short_name"])
    flavor, axis, idx = m[1], m[2], int(m[3])

    # The CSR access itself is on the secure PEB plane (bit-53 PEB aperture):
    flags = {"PEB": 1}                                   # SECURE_INT + PEB_APB_IO
    # EW links exist on both C_DIE and H_DIE -> cross-chip mesh edge (NEIGHBOR_ROUTE):
    if axis == "EW":
        flags["NEIGHBOR_ROUTE"] = 1                      # [52] engage neighbor path
        flags["EXIT_DIE"]       = 0                      # stays toward package mesh edge
    # NS links are C_DIE-only -> inter-die hop within the package (EXIT_DIE):
    else:  # NS
        flags["EXIT_DIE"]       = 1                      # [51] cross the originating die
        flags["NEIGHBOR_ROUTE"] = 0
    flags["EXIT_SENG"] = 1                               # [50] leave the local SEngine domain
    # NOTE: EW=cross-chip / NS=inter-die is [MED·INFERRED] (placement HIGH, wiring not traced).
    return {"die": die, "seng": seng, "flavor": flavor, "axis": axis,
            "idx": idx, "neighbor_flags": flags}

def neighbor_addr(local_addr, flags):                    # bit-insert per addr-decode SET macros
    a = local_addr & ~(0xF << 50)                        # clear EXIT_SENG..PEB window [53:50]
    a |= flags.get("EXIT_SENG",      0) << 50
    a |= flags.get("EXIT_DIE",       0) << 51
    a |= flags.get("NEIGHBOR_ROUTE", 0) << 52
    a |= flags.get("PEB",            0) << 53
    return a
```

---

## 12. Confidence ledger

**HIGH · OBSERVED**
* Safe load (`len == 323,198`, RestrictedUnpickler never resolved a global).
* PCIE 50 = `USER_PCIEA` (24) + `SECURE_PCIEM` (24) + 2 view nodes; 8 endpoints/view; bases
  `0x1000…`/`0x3000…` by nibble; each endpoint = NODE (`pciea/pciem_address_map.json`) +
  `BAR_OFFSET` (+0x0) + `MSIX_DOORBELL` (+0x2000000000000, bit 49), both `reserved.json`;
  schema byte-exact on disk.
* d2d = UCIE: 39 distinct links (A-EW 18, A-NS 8, S-EW 8, S-NS 5); 440 top-containers
  (C_DIE 296, H_DIE 144); each link = `D2D_TL` (credit transport) + `D2D_LL` (RX/TX) + 2nm
  UCIE PHY (`d2c_ucie2phy_top_{a,s}_2nm_ew`) + LTSM + BIST + ELA + ERG; schemas verified.
* Two-plane: real UCIE CSRs in `SECURE_INT` PEB plane (`PEB_APB_IO` + `PEB_APB_IO_BCAST`,
  +0x20000000 dual aperture); PCIe apertures in the dedicated PCIe views.
* d2d QSPI/PLL sideband = 432, on all 3 dies (C/H/IO, 144 each).
* UCIE TL = credit-based VC transport `{wreq,rreq,wdat,rdat,cmp}` + token-bucket rate limiter
  + `sec_ovrd` + recal handshake + `s2s_bridge_ch_split` (regs read on disk).
* Fabric: `TILE_FABRIC` (9 read + 9 write URB + embedded `HBM_XBAR_4X2`), IO/PEB/SP fabrics;
  HBM crossbar NoC = `HBM_XBAR_8X32` (secure) + `4X2` (tile) + `xbar_port_{hbm,fab}_{rd,wr}`.
* No PCIe/UCIE controller CSR node (`pcie5`/`snps`/`mrvl`/`xsr`/`e32mp`/`appaxi`/`iatu`/`noc`
  all = 0). Record arithmetic closes exactly; `.json` mirror counts byte-identical.

**MED · INFERRED**
* A = Application / S = ? and EW = cross-chip-mesh / NS = inter-die routing are name+topology
  inferences (placement HIGH; the exact wiring not traced).
* QSPI = UCIE sideband-config interface; `PLL_D2D` = d2d clock (name+size inference).
* The iATU/BAR inbound-translation hardware is absent from this DB; the aperture windows here
  are its SoC-side targets.

**LOW · NOTED / INFERRED**
* Whether firmware treats UCIE-A vs UCIE-S as distinct link classes or just two PHY flavors
  of one fabric is a firmware question; the address map shows two wrapper schemas differing
  only in the PHY macro. The Maverick v5 microarchitectural interior (3-die wiring, link
  steering) is `[INFERRED]` — the DB structure is `[OBSERVED]`.

---

*Source: `al_address_map_db.pkl` / `.json` (MAVERICK, NC-v5) +
`maverick/vpc-mirror/arch-regs/src/{address_map,csrs/d2d_top}/*.json` +
`maverick_pcie_bar_mapping.yaml`. Cross-gen reference: Cayman NC-v3 host BAR layout.
Lawful-interoperability static analysis.*

**See also:** [pkl-db](pkl-db.md) (load primitive + DB invariants) ·
[addr-decode](addr-decode.md) (the NEIGHBOR decoder) ·
[pcie-bars](pcie-bars.md) (host BAR layout) ·
[rdma-cross-die](../../dma/rdma-cross-die.md) (the d2d RDMA transport) ·
[hbm-d2d-pcie-blocks](../csr/hbm-d2d-pcie-blocks.md) (d2d/PCIe CSR blocks) ·
[pcie-hbm-tpb-d2d-triggers](../interrupt/pcie-hbm-tpb-d2d-triggers.md) (d2d interrupt triggers).
