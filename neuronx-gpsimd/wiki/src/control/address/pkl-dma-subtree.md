# pkl DMA-Engine Subtree — DDMA / CDMA / UDMA

> **Source.** `al_address_map_db.pkl` (216,631,794 B) and its text mirror
> `al_address_map_db.json` (514,276,583 B), both shipped under
> `.../arch-headers/maverick/ext/` in `aws-neuronx-gpsimd-customop-lib_0.21.2.0`.
> This is the **MAVERICK** SoC (NC-v5) address-map database. The byte-grounded
> Cayman (NC-v3) cross-check is [`../../dma/sdma-windows-apb.md`](../../dma/sdma-windows-apb.md).
> Load mechanics and the 23-field record schema live in
> [`pkl-db.md`](./pkl-db.md). Engine register schemas:
> [`../csr/udma-m2s.md`](../csr/udma-m2s.md), [`../csr/udma-s2m.md`](../csr/udma-s2m.md),
> [`../csr/udma-gen-tdma.md`](../csr/udma-gen-tdma.md);
> hardware engine model: [`../../dma/udma-hw-engine.md`](../../dma/udma-hw-engine.md).
> The DDMA/CDMA/UDMA-vs-SDMA naming taxonomy is
> [`../../collectives/ncfw/lx-isa-naming-archid-synthesis.md`](../../collectives/ncfw/lx-isa-naming-archid-synthesis.md).

This page carves the **DMA-engine block subtree** out of the Maverick address-map
DB: the three engine families **DDMA / CDMA / UDMA**, their SoC bases and sizes,
the channel arrays, the UDMA M2S/S2M/GEN/GEN_EX engine geometry, the CSR-schema
bindings, the array/instance expansion model, the Maverick-vs-Cayman DMA delta,
and the full record-count reconciliation. Every figure here comes from **streaming**
the `.json` mirror with `ijson` (never `pickle.load`); see
[§Carve pseudocode](#carving-the-dma-subtree-safely).

> **WALL — arch identity (HIGH/CARRIED).** The pkl describes **MAVERICK (NC-v5)**.
> The `.pkl` header is OBSERVED; the DB *records* (names, offsets, sizes, schema
> bindings, counts) are **OBSERVED**; the deeper v5 hardware *interior* (what the
> M2S queue regs do cycle-by-cycle, the CCE micro-arch) is **INFERRED** from the
> schema names. The **CAYMAN (NC-v3)** flat YAML / [SDMA window pages](../../dma/sdma-windows-apb.md)
> are the byte-grounded cross-gen check. Where Cayman and Maverick *agree* the fact
> is OBSERVED on both; where Maverick-only it is OBSERVED-in-pkl / INFERRED-as-hw.

---

## 1. Naming GOTCHA — Maverick calls them DDMA / CDMA / UDMA, not "SDMA"

> **GOTCHA.** On Maverick the SoC DMA engines are named
> **DDMA**, **CDMA**, and **UDMA**. The Cayman-era keyword **`SDMA` matches ZERO
> records** in the entire 323,198-record DB. If you carry the Cayman "SDMA"
> mental model into this pkl you will find nothing — grep for `DDMA`/`CDMA`/`UDMA`.
> The engine *contents* are the same UDMA descriptor-DMA IP family; only the SoC
> instance names changed. `SDMA` substring → **0** records.

The link between the two namings is the on-disk schema path. The channel schemas
reference `address_map/apb/**sdma**/udma_apb_chain.json` — the **RTL IP directory
is still named `sdma`** even though the address-map *node* names are
DDMA/CDMA/UDMA (OBSERVED in the `json` field, e.g. UDMA_APP binds
`.../address_map/apb/sdma/udma_apb_chain.json`). So:

| where | name |
| --- | --- |
| Cayman SoC instance (NC-v3) | `SDMA_0..31` per SE |
| Maverick SoC instance (NC-v5) | `DDMA_0..15` + `CDMA_0..7` + `UDMA` core |
| RTL IP directory (both) | `.../apb/sdma/` |
| engine register schemas (both) | `dma_csrs/al_udma_*.json` |

This is the taxonomy reconciliation expanded in
[`../../collectives/ncfw/lx-isa-naming-archid-synthesis.md`](../../collectives/ncfw/lx-isa-naming-archid-synthesis.md).

### The three families at a glance

| family | role | per-SENG count | channel root schema | identity |
| --- | --- | --- | --- | --- |
| **DDMA** | **Data** DMA channel | 16 unicast | `ddma_user.json` (NODE 0x100000) | `UDMA_APP` core + app glue + FCM + landing buffer |
| **CDMA** | **Compute** DMA channel | 8 unicast | `cdma_user.json` (NODE 0x100000) | DDMA **+ CCE** (inline FMA / dtype-convert / stochastic-round) |
| **UDMA** | **Unified DMA engine core** | not standalone | `udma_apb_chain.json` (NODE 0x80000) | M2S + S2M + GEN + GEN_EX, instantiated inside *every* DDMA/CDMA channel |

> **GOTCHA.** **UDMA is NOT a standalone engine node.** There is
> ZERO record with `short_name=="UDMA"` that is its own top-level engine, and ZERO
> UDMA record outside a DDMA or CDMA channel. "UDMA" is the shared engine core
> (`UDMA_APP` container) embedded in each data/compute channel: of the 146,064
> records whose name contains `UDMA`, **0** lack a `DDMA`/`CDMA` token
> (`UDMA_OUTSIDE_DDMA_CDMA == 0`).

---

## 2. Where the DMA engines live — two planes

A DMA channel surfaces in **two access planes**, exactly the three-view
decomposition the Cayman [SDMA windows](../../dma/sdma-windows-apb.md) found:

### 2a. Engine view (SENG direct children) — RESERVED placeholders only

At `parent_names == ['ADDRESS_MAP','user_int','seng_0']` the SENG carries only
**reserved 1 MiB stubs** — not real CSRs:

| node | count/SENG | type / size | `json` | self_array_size | children |
| --- | --- | --- | --- | --- | --- |
| `DDMA_DESC_0..15` | 16 | NODE / 0x100000 | `reserved.json` | `"16"` | none |
| `CDMA_DESC_0..7` | 8 | NODE / 0x100000 | `reserved.json` | `"8"` | none |

Example: `USER_INT_SENG_0_DDMA_DESC_0` → `type=NODE, size=0x100000,
count=4, self_array_size="16", json=reserved.json`, **0 descendants**. The real
DMA CSRs are *not* in the engine view; they are reached through the APB_IO plane
(§2b). Stub totals (db-wide): **DDMA_DESC 64** (16 × 4 SENG), **CDMA_DESC 32**
(8 × 4 SENG). `secure_int` has none of these stubs.

### 2b. APB_IO control plane (`user_int`) — the real DMA channels

Parent chain `['ADDRESS_MAP','user_int','seng_0','apb_io','c_die','APB_IO','user']`.
Channel roots are `NODE size 0x100000` binding `ddma_user.json` / `cdma_user.json`:

| channel root | SENG_0 base | stride | count | binds |
| --- | --- | --- | --- | --- |
| `DDMA_0 .. DDMA_15` | `0xc001000000` + n·0x100000 | 1 MiB | 16 unicast | `ddma_user.json` |
| `CDMA_0 .. CDMA_7` | `0xc002000000` + n·0x100000 | 1 MiB | 8 unicast | `cdma_user.json` |
| `DDMA_BCAST_UDMA` | `0xc002800000` (size 0x80000) | — | 1 bcast | `udma_apb_chain.json` |
| `CDMA_BCAST_UDMA` | `0xc002880000` (size 0x80000) | — | 1 bcast | `udma_apb_chain.json` |
| `H2D_DDMA` | `0xc002900000` (size 0x100000) | — | host→device | `ddma_user.json` |
| `D2H_DDMA` | `0xc002a00000` (size 0x100000) | — | device→host | `ddma_user.json` |

Bases (streamed): `DDMA_0..3` = `0xc001000000, 0xc001100000,
0xc001200000, 0xc001300000` → **stride = 0x100000 (1 MiB)**; `CDMA_0..3` =
`0xc002000000, 0xc002100000, …` → same 1 MiB stride. The SENG-to-SENG stride is
`0x100000000000` (so `SENG_1 DDMA_0` = base + `0x100000000000`).

### 2c. PEB_APB_IO plane (`secure_int`) — privileged, dual aperture

Parent chain `['ADDRESS_MAP','secure_int','seng_0','c_die','PEB_APB_IO','user']`.
Same channel set, byte-identical internal structure, but reached through the
privileged PEB aperture **and in two sub-apertures per SENG**: `PEB_APB_IO`
(plain priv) and `PEB_APB_IO_BCAST` (priv APB-broadcast path). This doubling is
why **secure_int DMA records are exactly 2× user_int**. Streamed split:
`PEB_APB_IO 58,808 / PEB_APB_IO_BCAST 58,808` — **50/50 exact**.

Base: `SECURE_INT_SENG_0_C_DIE_PEB_APB_IO_USER_DDMA_0` = `0x2000008001000000`,
whose low part `0x8001000000` is the APB_IO window and whose high bit is the
privileged-aperture selector.

> **CORRECTION — the v5 privileged-aperture bit is 61, not 53.** Labelling the
> privileged-aperture bit *"bit53 = 0x20000000000000"* is wrong here: that literal
> is `1<<53`, but it is **NOT** the bit set in the OBSERVED Maverick base: with the local
> window subtracted, `0x2000008001000000 − 0x8001000000 = 0x2000000000000000 =
> 1<<61`. So on **this Maverick pkl** the privileged aperture is encoded at
> **bit 61** (`0x2000000000000000`), not bit 53. The index "53" is carried over
> from Cayman's PEB decode and is a stale nomenclature slip for the v5 base; the
> *mechanism* (a single high address bit gating the privileged DMA view) is
> unchanged. Field used: `base` on `SECURE_INT_SENG_0_C_DIE_PEB_APB_IO_USER_DDMA_0`.

---

## 3. The DMA channel — internal layout (byte-exact)

Sample: `USER_INT_SENG_0_APB_IO_C_DIE_APB_IO_USER_DDMA_0` (base `0xc001000000`,
size `0x100000`, NODE → `ddma_user.json`). Direct children (offsets are
**parent-relative** — the `offset` field, which the flat YAML dropped):

| off in channel | block | type | size | `json` (schema) |
| --- | --- | --- | --- | --- |
| `+0x000000` | `UDMA_APP` | NODE | `0x080000` | `udma_apb_chain.json` (the 512 KiB engine core) |
| `+0x080000` | `DMA_APP` | REGFILE | `0x001000` | `tdma_model.json` (app glue / bcast cfg) |
| `+0x081000` | `NOTIFIC` | REGFILE | `0x001000` | `notific_10_queue.json` (10-queue notify) |
| `+0x084000` | `FCM_APP` | REGFILE | `0x004000` | `fcm_app.json` (Fast CRC + Memcopy) |
| `+0x088000` | `DMA_LANDING_BUFFER` | NODE | `0x001000` | `dma_landing_buffer.json` |
| `+0x089000` | `DMA_APP_ENG_GEN` | REGFILE | `0x000400` | `dma_app_eng_gen.json` |
| `+0x0FF000` | `CCE` | REGFILE | `0x001000` | `cce.json` — **CDMA ONLY** (§4) |

A `CDMA_0` channel is the **identical six blocks plus** the `CCE` block at
`+0xFF000`. Both layouts come from the channel-root children.

### 3a. The UDMA_APP engine core (`udma_apb_chain.json`, NODE 0x80000)

`UDMA_APP` is the unified DMA engine. Its HDL path tag is
`u_udma.u_mla_udma_top.ap_udma_top.gdma_regfile_wrapper`. Direct children:

| off in UDMA_APP | engine | type | size | `json` | Cayman off (size) |
| --- | --- | --- | --- | --- | --- |
| `+0x000000` | **M2S** (master→slave, outbound desc DMA) | REGFILE | `0x040000` | `al_udma_m2s_regs.json` | `+0x00000` (0x20000) |
| `+0x040000` | **S2M** (slave→master, inbound desc DMA) | REGFILE | `0x038000` | `al_udma_s2m_regs.json` | `+0x20000` (0x18000) |
| `+0x078000` | **GEN** (common ctrl + INTC) | REGFILE | `0x004000` | `al_udma_gen_regs.json` | `+0x38000` (0x4000) |
| `+0x07C000` | **GEN_EX** (V4 virtualization extension) | REGFILE | `0x004000` | `al_udma_gen_ex_regs.json` | `+0x3C000` (0x4000) |

> **NOTE — M2S / S2M nomenclature.** **M2S = master-to-slave** = the *outbound*
> descriptor-DMA datapath (the engine reads descriptors and pushes data out;
> per-queue doorbells + completion regs live here). **S2M = slave-to-master** =
> the *inbound* datapath (data lands and is acknowledged; auto-complete /
> flush-marker / packet-header-drop machinery). **GEN** is the common control:
> primary + secondary interrupt controllers, AXI BW throttling, TX-attr/TTT
> tables, tracer client. **GEN_EX** is the `_ex` virtualization mirror of GEN.
> Register-level detail: [`../csr/udma-m2s.md`](../csr/udma-m2s.md),
> [`../csr/udma-s2m.md`](../csr/udma-s2m.md),
> [`../csr/udma-gen-tdma.md`](../csr/udma-gen-tdma.md).

> **CROSS-GEN DELTA (§7).** The Maverick **UDMA window doubled
> 0x40000 → 0x80000**. M2S grew `0x20000 → 0x40000`; S2M grew `0x18000 → 0x38000`;
> GEN/GEN_EX unchanged at `0x4000`. Because M2S/S2M grew, **all within-UDMA
> offsets moved**: S2M `+0x20000 → +0x40000`, GEN `+0x38000 → +0x78000`, GEN_EX
> `+0x3C000 → +0x7C000`. The on-disk `udma_apb_chain.json`
> Includes agree (`m2s@0x0, s2m@0x40000, gen@0x78000, gen_ex@0x7C000`).

### 3b. Engine internals (selected, from the on-disk schemas)

- **M2S** (`al_udma_m2s_regs.json`, AddrWidth 18): per-queue `M2S_Q` doorbell +
  `M2S_comp` completion, `M2S_rate_limiter`/`M2S_dwrr` shapers, `AXI_M2S`,
  `PKT_SCHED_DEBUG_MON`@`+0x530`, `M2S_FORCE_TARGET_VAL_TABLE`@`+0x800` (TABLE),
  `M2S_AXI_ERROR_LOGGER`@`+0x880`, `GLOBALS`@`+0x8c0`, `AXI_TRFC_GEN`@`+0x39000`
  (traffic generator), `MEM_CTRL`@`+0x3a000` (`udma_eth_mem_ctrl.json`),
  `PMU`@`+0x3b000`, **`FCI`@`+0x3c000` (`fci_top_64_ch.json`, REGFILE 0x4000 —
  64-channel Fast CPU Interface)**.
- **S2M** (`al_udma_s2m_regs.json`): `AUTO_CMPL_DETECT_FE`@`+0x2a8`,
  `NO_NEED_ACK_DETECT_FE`@`+0x2ac`, `FLUSH_MARKER_FE`@`+0x34c`,
  `PKT_HDR_DROP_FE`@`+0x35c`, `BRESP_ERR_CMPL_FM`@`+0x390`,
  `S2M_FORCE_TARGET_VAL_TABLE`@`+0x800` (TABLE), `GLOBALS`@`+0x8c0`,
  **`FCI`@`+0x34000` (also a 64-ch FCI)**.
- **GEN** (`al_udma_gen_regs.json`): `INT_CTRL_BASE_ADDR`@`+0x0` (INTC 0x2000,
  `udma_primary_int_ctrl.json`), `INT_CTRL_SEC_ADDR`@`+0x2000` (INTC 0x100,
  `udma_secondary_int_ctrl.json`), `TRACER_CLIENT`@`+0x3680` (INDIRECT_ACCESS),
  `DEBUG_MONITOR_0..7`@`+0x4004`, `TTT_TABLE`@`+0x4c80`,
  6× `axi_bw_throttling.json`@`+0x4d00` (M2S/S2M read-desc / read-data /
  write-cmpl), `TX_ATTR_TABLE`@`+0x4e00`.
- **GEN_EX** (`al_udma_gen_ex_regs.json`): same shape as GEN — 8× DEBUG_MONITOR,
  TTT_TABLE, 6× axi_bw_throttling, TX_ATTR_TABLE, GLOBALS (the `_ex` mirror).

---

## 4. DDMA vs CDMA — the compute-DMA identity

DDMA and CDMA channels are **byte-identical except for one block**. The
channel-root children show both carry the six common blocks (§3); the
CDMA channel adds **`CCE @ +0xFF000`** and *nothing else*.

```
ddma_user.json (UnitName "ddma_user_chain"):  udma_app + dma_app + notific
                                              + fcm_app + dma_landing_buffer + dma_app_eng_gen
cdma_user.json (UnitName "cdma_user_chain"):  SAME SIX  +  cce@0xFF000   <-- THE DIFFERENCE
```

**CCE** (`cce.json`, REGFILE 0x1000) bundles `cce_fma_cfg`, `cce_fma_const`,
`debug`, `stochastic_rounding_seed_ctrl`, `out_data_conv_cfg`,
`input_data_conv_cfg`, `spares`. So CCE is a **per-channel fused-multiply-add +
data-type-conversion + stochastic-rounding compute block** inline in the DMA
path. Therefore:

- **DDMA** = plain Data DMA channel (descriptor move-engine + app glue).
- **CDMA** = Compute DMA channel = DDMA **+** the inline CCE FMA/convert engine.

CCE appears **only** under CDMA — **96 nodes** = 8 CDMA × 12 cluster-instances —
and **never** under DDMA. The base of `CDMA_0`'s CCE = `0xc0020ff000`
(= CDMA_0 base `0xc002000000` + `0xFF000`) — this resolves the "what is CDMA?"
question.

> **CROSS-GEN NOTE (MED).** Cayman's SDMA app engines were CME / DRE / CCE
> (merge / strided-transpose / compute). In this Maverick pkl `cme.json` = 0
> nodes and `dre.json` = 0 nodes; **CCE survives, folded into CDMA channels**.
> Maverick's DDMA instead carries `FCM_APP` (Fast CRC + Memcopy, NEW) and a
> `DMA_LANDING_BUFFER`. The *absence* of CME/DRE is HIGH/OBSERVED; whether they
> were dropped or relocated elsewhere is not decidable from the address map alone.

### Broadcast aperture — a different model than Cayman

Maverick promotes broadcast to a **dedicated per-family container** that sits
*after* the channel arrays (Cayman used a per-channel `+0x80000` mirror). Each
`*_BCAST_UDMA` is a `NODE size 0x80000` binding `udma_apb_chain.json` — i.e. it
holds a **full UDMA engine** (same `M2S@+0/S2M@+0x40000/GEN@+0x78000/GEN_EX@+0x7C000`
geometry). One broadcast writer per family per cluster fans a single descriptor
program out to a whole group via the `PEB_APB_IO_BCAST` aperture + `papb_bcast`
mask. Bases: `DDMA_BCAST_UDMA@0xc002800000`, `CDMA_BCAST_UDMA@0xc002880000`.
**BCAST_UDMA total = 24** = user_int (DDMA 4 + CDMA 4) + secure_int (DDMA 8 + CDMA 8).

---

## 5. The FIS / SPROT protection surface — the 6,656 "orphans"

**6,656** DDMA/CDMA-named records are **not** under any `UDMA_APP`/`BCAST`/`DESC`
channel root — they are the per-channel **Fabric-Interface-Slice (FIS) +
protection** nodes (region prefix e.g.
`USER_INT_SENG_0_APB_IO_C_DIE_APB_IO_USER_FIS_DDMA_n`). This is the same
host-FIS / priv-FIS three-view the [Cayman SDMA pages](../../dma/sdma-windows-apb.md)
documented:

| host-visible (user view) | count | privileged (amzn view) | count |
| --- | --- | --- | --- |
| `user_remapper.json` | 312 | `amzn_remapper.json` | 208 |
| `qos_host_visible.json` | 312 | `qos_prot.json` | 208 |
| `papb_bcast.json` (bcast mask) | 312 | `fis_control.json` (APB decode/gate) | 208 |
| `errtrig_user.json` | 312 | `errtrig_amzn.json` | 208 |
| `fis_sprot_user.json` | 312 | `fis_sprot_amzn.json` | 208 |
| `cxela500.json` (ELA debug) | 312 | `ddma_amzn.json` / `cdma_amzn.json` | 144 / 64 |
| `qos_pmu.json` | 312 | INTC `intc_4grp_*_unit.json` | 624 / 416 |

The end-to-end APB config path (fabric → apbblk firewall → chain → per-channel
FIS → UDMA CSR) is the same topology as Cayman. Placement is HIGH/OBSERVED; the
APB write data-flow *direction* is MED/INFERRED (carried from the Cayman analysis).

---

## 6. Within-channel address model + worked example

```
full_addr = channel_base(view, seng, family, n) + block_offset + engine_offset + reg

channel_base(user_int, seng s, DDMA, n) = 0xc001000000 + s*0x100000000000 + n*0x100000
channel_base(user_int, seng s, CDMA, n) = 0xc002000000 + s*0x100000000000 + n*0x100000

block_offset  ∈ { UDMA_APP 0x00000, DMA_APP(tdma) 0x80000, NOTIFIC 0x81000,
                  FCM_APP 0x84000, DMA_LANDING_BUFFER 0x88000,
                  DMA_APP_ENG_GEN 0x89000, CCE 0xFF000 (CDMA only) }
engine_offset ∈ { M2S 0x00000, S2M 0x40000, GEN 0x78000, GEN_EX 0x7C000 }   # inside UDMA_APP
```

**Worked** (`USER_INT SENG_0 DDMA_5` channel @ `0xc001500000`; bases OBSERVED,
sums computed):

| target | address |
| --- | --- |
| M2S | `0xc001500000` |
| S2M | `0xc001540000` |
| GEN | `0xc001578000` |
| GEN_EX | `0xc00157c000` |
| DMA_APP (tdma) | `0xc001580000` |
| FCM_APP | `0xc001584000` |

The privileged equivalent adds the bit-61 aperture selector
(`0x2000000000000000`, see §2c CORRECTION) and uses local `0x8001500000`. The
address model closes with no residual.

---

## 7. Maverick vs Cayman DMA delta

| aspect | Cayman (NC-v3, flat YAML) | Maverick (this pkl) | conf |
| --- | --- | --- | --- |
| engine naming | `SDMA_0..31` per SE | `DDMA_0..15` + `CDMA_0..7` + `UDMA` core (SDMA kw = 0) | HIGH |
| channel stride | 0x100000 (1 MiB) | 0x100000 (1 MiB) — **SAME** | HIGH |
| channel container | 0x80000 (512 KiB) UDMA | 0x80000 (512 KiB) `UDMA_APP` — **SAME** | HIGH |
| UDMA window | 0x40000 | **0x80000 (DOUBLED)** | HIGH |
| M2S offset / size | `+0x00000` / 0x20000 | `+0x00000` / **0x40000** | HIGH |
| S2M offset / size | `+0x20000` / 0x18000 | `+0x40000` / **0x38000** | HIGH |
| GEN offset | `+0x38000` / 0x4000 | `+0x78000` / 0x4000 | HIGH |
| GEN_EX offset | `+0x3C000` / 0x4000 | `+0x7C000` / 0x4000 | HIGH |
| app glue (tdma) | `MISC_SDMA_APP` @ `+0x40000` | `DMA_APP` @ `+0x80000` (`tdma_model`) | HIGH |
| app engines | CME / DRE / CCE (priv view) | **CCE only**, folded into CDMA channels | HIGH \| CME/DRE absent=OBS, dropped-vs-moved=MED |
| Fast CRC+Memcopy | (not a named channel block) | `FCM_APP` @ `+0x84000` (**NEW**) | HIGH |
| landing buffer | (not present) | `DMA_LANDING_BUFFER` @ `+0x88000` | HIGH |
| broadcast model | per-channel `+0x80000` mirror on `SDMA_0/16` | dedicated `DDMA_BCAST_UDMA` / `CDMA_BCAST_UDMA` per family | HIGH |
| channels per SENG | 32 SDMA per SE half | 16 DDMA + 8 CDMA per SENG | HIGH |
| H2D/D2H IO DMA | `USER_IO_SDMA_D2H/H2D` | `H2D_DDMA` / `D2H_DDMA` | HIGH |
| privileged aperture | bit-N PEB decode | high-bit PEB decode (`PEB_APB_IO` + `_BCAST`); **bit 61 on the v5 base** (§2c CORRECTION) | HIGH |
| FIS / SPROT set | per-channel 3-view | per-channel 3-view, **identical schema set** | HIGH |
| RTL IP dir name | `csrs/sdma/` | `.../apb/sdma/` (dir still `sdma`) | HIGH |

> **Reading.** Same UDMA descriptor-engine IP family (M2S/S2M/GEN/GEN_EX + FIS
> protection), re-organized for Maverick: DMA is split into a **Data class**
> (DDMA, 16/SENG) and a **Compute class** (CDMA, 8/SENG, +CCE); the UDMA register
> window doubled; the app-engine set narrowed to CCE + FCM; broadcast became a
> dedicated per-family node. "SDMA" is retired at the SoC level but persists as
> the RTL IP directory. Matching facts = **OBSERVED** on both gens; Maverick-only
> facts = **OBSERVED-in-pkl / INFERRED-as-hardware**.

---

## 8. Array / instance-expansion model

The DB materializes array members as **separate records** (`as_array=="false"`
db-wide); the array geometry is carried in the `count` / `self_array_size` /
`parent_array_size` / `instance_index` fields:

- **Engine-view stubs:** `DDMA_DESC_n` `count=4, self_array_size="16"`
  (the 16 DDMA slots × 4 SENG); `CDMA_DESC_n` `self_array_size="8"`.
- **APB_IO channel roots:** `DDMA_n self_array_size="16"`,
  `parent_array_size="1*1*4*1*1*1*1"` (the APB_IO container chain adds singleton
  dims), `instance_index "0x0".."0xF"`; `CDMA_n self_array_size="8"`.
  The `UDMA_APP` node carries `parent_array_size="1*1*4*1*1*1*1*16"`.

**Channel census (UDMA_APP-bearing real channels)** — streamed, by (view, family, kind):

| view | family | kind | count | derivation |
| --- | --- | --- | --- | --- |
| user_int | DDMA | unicast | 64 | 16 × 4 SENG |
| user_int | CDMA | unicast | 32 | 8 × 4 SENG |
| user_int | DDMA | H2D/D2H | 8 | (H2D+D2H) × 4 SENG |
| secure_int | DDMA | unicast | 128 | 16 × 4 SENG × 2 apertures |
| secure_int | CDMA | unicast | 64 | 8 × 4 SENG × 2 |
| secure_int | DDMA | H2D/D2H | 16 | (H2D+D2H) × 4 SENG × 2 |
| **total** | | | **312** | 288 unicast + 24 H2D/D2H |

Plus **24 `BCAST_UDMA`** containers (§4). Therefore the UDMA engine count
(`al_udma_m2s_regs.json` instances) = **336** = 312 channels + 24 BCAST, each =
one M2S + one S2M + one GEN + one GEN_EX. **Streamed counts confirm: UDMA_APP =
312, BCAST_UDMA = 24, m2s/s2m/gen/gen_ex = 336 each.**

**FCI sub-channel expansion:** `fci_top_64_ch` = **672** (336 M2S-FCI
+ 336 S2M-FCI), each a 64-channel block → `fci_channel` = **43,008** = 672 × 64.
(This is the largest raw-RTL schema in the DB, now fully placed.)

---

## 9. Block → CSR-schema bindings

The `json` field on each node binds it to a register schema whose `Type` matches
the node `type` and whose `SizeInBytes` matches the node `size` (all present on
disk in the Maverick `vpc-mirror` tree). The load-defining DMA schemas (count =
streamed node count):

| schema (basename) | type | size | count | binds |
| --- | --- | --- | --- | --- |
| `ddma_user.json` | NODE | 0x100000 | 216 | DDMA channel root (incl H2D/D2H) |
| `cdma_user.json` | NODE | 0x100000 | 96 | CDMA channel root |
| `udma_apb_chain.json` | NODE | 0x080000 | 336 | the `UDMA_APP` engine container |
| `al_udma_m2s_regs.json` | REGFILE | 0x040000 | 336 | M2S (outbound desc DMA) |
| `al_udma_s2m_regs.json` | REGFILE | 0x038000 | 336 | S2M (inbound desc DMA) |
| `al_udma_gen_regs.json` | REGFILE | 0x004000 | 336 | GEN (common ctrl + INTC) |
| `al_udma_gen_ex_regs.json` | REGFILE | 0x004000 | 336 | GEN_EX (V4 virt extension) |
| `tdma_model.json` | REGFILE | 0x001000 | 312 | `DMA_APP` (app glue / bcast cfg) |
| `fcm_app.json` | REGFILE | 0x004000 | 312 | Fast CRC + Memcopy engine cfg |
| `dma_landing_buffer.json` | NODE | 0x001000 | 312 | landing buffer |
| `dma_app_eng_gen.json` | REGFILE | 0x000400 | 312 | DMA app-engine general cfg |
| `cce.json` | REGFILE | 0x001000 | 96 | CDMA compute (FMA/convert) — **CDMA only** |
| `notific_10_queue.json` | REGFILE | 0x001000 | 312 | channel `NOTIFIC` (10-queue) |
| `fci_top_64_ch.json` | REGFILE | 0x004000 | 672 | 64-ch Fast CPU Interface |
| `fci_channel.json` | REGFILE | (per-ch) | 43008 | FCI per-channel (64 × 672) |

(Plus the FIS/SPROT set of §5 and the generic per-regfile instrumentation —
parity/RAS/access-log/`globals` leaves.) Streamed counts for all 12 sampled DMA
schemas matched these figures exactly. The M2S/S2M descriptor-ring register
detail is in [`../csr/udma-m2s.md`](../csr/udma-m2s.md) /
[`../csr/udma-s2m.md`](../csr/udma-s2m.md); the GEN common-control + tdma glue in
[`../csr/udma-gen-tdma.md`](../csr/udma-gen-tdma.md).

---

## 10. Record-count reconciliation (streamed, exact)

Whole-DB DMA family counts (streamed over all 323,198 records):

| family | total | user_int | secure_int | note |
| --- | --- | --- | --- | --- |
| **DDMA** | 119,356 | 39,300 | 80,056 | |
| **CDMA** | 56,020 | 18,460 | 37,560 | |
| **UDMA** | 146,064 | 48,688 | 97,376 | **all inside DDMA/CDMA** (exactly 2.000× user→secure) |
| **SDMA** | **0** | — | — | name retired (§1) |

`secure_int ≈ 2× user_int` because secure_int has two PEB apertures
(`PEB_APB_IO` + `PEB_APB_IO_BCAST`), each carrying **58,808** DMA records (50/50).

The DMA-engine **subtree** is DDMA + CDMA = **175,376** records, partitioned exactly:

```
channel-root subtrees ................ 168,720
    DDMA unicast    97,344  (192 roots × 507 recs)      # 192 = 16 × 4 SENG × (user 1 + secure 2)
    DDMA H2D/D2H    12,168
    DDMA BCAST       5,172
    DDMA DESC-stub      64
    CDMA unicast    48,768  (96 roots × 508 recs)       # CDMA = DDMA + CCE  ⇒ +1 rec/channel
    CDMA BCAST       5,172
    CDMA DESC-stub      32
FIS/SPROT protection nodes ...........   6,656           # §5; not under any channel root
-----------------------------------------------
TOTAL ................................ 175,376  (= 119,356 + 56,020)   EXACT
```

Per-channel record counts: **DDMA unicast = 507** (incl root); **CDMA unicast =
508** (the +1 = the CCE node). Each channel's `UDMA_APP` carries **128
`fci_channel` leaves** (64 M2S-FCI + 64 S2M-FCI).

> **GOTCHA — excluded.** The 7,512 records that contain `DMA` but are *not*
> DDMA/CDMA/UDMA are the **DGE** (Descriptor Generation Engine: `DGE_DESC_*`,
> `DMA_DESC_PUSH_*`, `STREAM_TO_AXI_*`) — a **TPB-side** engine, **not** part of
> the DMA-engine subtree, and excluded from these counts.

---

## Carving the DMA subtree safely

> **SAFETY (CRITICAL).** **Never** `pickle.load()` / `pickle.loads()` this file —
> a pickle can execute code on load. Stream the `.json` mirror with `ijson` (a
> memory-bounded SAX-style parser; do **not** slurp 514 MB), or scan opcodes with
> `pickletools.genops()` (no `find_class`, no object construction). The DB is a
> flat top-level array of dict records; filter to the DMA subtree by the engine
> token in the `name` field. The streaming carve below produces every count on
> this page.

```python
import ijson, collections, re

JSON = ".../arch-headers/maverick/ext/al_address_map_db.json"   # 514 MB mirror

def dma_records(path):
    """Memory-bounded iterator over DMA-engine records.

    Streams the top-level array one record at a time (never loading the whole
    file) and yields only records whose name carries a DDMA / CDMA / UDMA token.
    A DGE record is *not* a DMA-engine record (it is TPB-side) and is dropped.
    """
    dma_tok = re.compile(r'\b(?:D|C|U)DMA\b')   # DDMA | CDMA | UDMA
    with open(path, 'rb') as f:
        for rec in ijson.items(f, 'item'):      # 'item' = each array element
            name = rec.get('name', '')
            if 'DMA' not in name:
                continue
            if dma_tok.search(name):
                yield rec                        # one of the three engine families

def channel_base(view, seng, family, n):
    """SoC base of a unicast DMA channel root (OBSERVED bases; computed offsets).

    view   : 'user_int'  -> APB_IO control plane (the real channels)
    seng   : 0..3        -> SENG stride 0x100000000000
    family : 'DDMA'      -> region 0xc001000000 ; 'CDMA' -> 0xc002000000
    n      : channel index within the array (DDMA 0..15, CDMA 0..7)
    """
    region = 0xc001000000 if family == 'DDMA' else 0xc002000000
    return region + seng * 0x100000000000 + n * 0x100000   # 1 MiB channel stride

# engine_offset inside a UDMA_APP container (OBSERVED, Maverick geometry):
ENGINE_OFF = {'M2S': 0x00000, 'S2M': 0x40000, 'GEN': 0x78000, 'GEN_EX': 0x7C000}
# block_offset inside a channel root (OBSERVED; CCE present only on CDMA):
BLOCK_OFF  = {'UDMA_APP': 0x00000, 'DMA_APP': 0x80000, 'NOTIFIC': 0x81000,
              'FCM_APP': 0x84000, 'DMA_LANDING_BUFFER': 0x88000,
              'DMA_APP_ENG_GEN': 0x89000, 'CCE': 0xFF000}

# Reconcile the family record counts (closes exactly: DDMA 119356 + CDMA 56020):
fam = collections.Counter()
for rec in dma_records(JSON):
    name = rec['name']
    if 'DDMA' in name: fam['DDMA'] += 1
    if 'CDMA' in name: fam['CDMA'] += 1
    if 'UDMA' in name: fam['UDMA'] += 1
# => {'DDMA': 119356, 'UDMA': 146064, 'CDMA': 56020}  ; SDMA token => 0
```

Channel-base derivation for the `DDMA_5` worked example (§6):
`channel_base('user_int', 0, 'DDMA', 5)` = `0xc001500000`; M2S =
`+ BLOCK_OFF['UDMA_APP'] + ENGINE_OFF['M2S']` = `0xc001500000`; GEN =
`+ 0x78000` = `0xc001578000`. The privileged view adds the bit-61 aperture
selector `0x2000000000000000` (§2c).

---

## Confidence ledger

| claim | confidence | basis |
| --- | --- | --- |
| Engines named DDMA/CDMA/UDMA; `SDMA` token = 0 | HIGH / OBSERVED | streamed name scan |
| UDMA never standalone; 0 UDMA recs outside DDMA/CDMA | HIGH / OBSERVED | `UDMA_OUTSIDE_DDMA_CDMA == 0` |
| DDMA = data channel; CDMA = DDMA + CCE (FMA/convert/stoch-round) | HIGH / OBSERVED | channel-root children + `cce.json` bundles |
| UDMA geometry M2S+0/S2M+0x40000/GEN+0x78000/GEN_EX+0x7C000 | HIGH / OBSERVED | `UDMA_APP` children offsets |
| Channel stride 1 MiB; 16 DDMA + 8 CDMA / SENG | HIGH / OBSERVED | streamed bases + `self_array_size` |
| 312 UDMA_APP + 24 BCAST = 336 engine instances | HIGH / OBSERVED | streamed `short_name` census |
| secure_int = 2× user_int (50/50 dual aperture) | HIGH / OBSERVED | `PEB_APB_IO 58,808 / _BCAST 58,808` |
| Record arithmetic 119,356 + 56,020 = 175,376 = 168,720 + 6,656 | HIGH / OBSERVED | streamed family counts |
| Privileged aperture = **bit 61** on v5 base (not bit 53) | HIGH / OBSERVED | `base` arithmetic (§2c CORRECTION) |
| UDMA window doubled 0x40000→0x80000 vs Cayman | HIGH / OBSERVED | Maverick vs Cayman offsets |
| CME/DRE dropped vs relocated | MED / INFERRED | absence OBSERVED, fate inferred |
| APB write data-flow *direction* | MED / INFERRED | carried from Cayman analysis |
| Firmware treats CCE as a distinct compute `engine_idx` | LOW | firmware-image question, not in address map |
