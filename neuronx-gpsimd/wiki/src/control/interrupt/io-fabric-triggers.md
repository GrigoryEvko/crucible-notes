# Interrupts — IO-Fabric Source Map

The IO-fabric interrupt-source map is the trigger vocabulary that latches into the
io-fabric APB INTC of the Cayman SoC: a flat sequence of **243** interrupt trigger
declarations recovered from `intc/io_fabric_triggers.yaml` (43955 bytes, 1310 lines),
each carrying a fixed 5-key schema and each cross-checked against a real CSR cause bit
in the `csrs/{iofabric,fis,apbblk,notific,intc,sprot}` register-description tree, the
`output/address_map/address_map_json_xref.yaml` block→JSON map, and the
`output/address_map/apb_chain_defs.vh` APB address-chain.

This page enumerates the source map **grouped by sub-block** with representative
entries and full per-group counts — it is not a 1310-line dump. For the underlying
INTC silicon these sources feed, see
[`../csr/intc-1group-apintc.md`](../csr/intc-1group-apintc.md) (the `intc_1grp`/`ap_intc`
IOFIC aggregators) and [`../csr/intc-4group.md`](../csr/intc-4group.md) (the 4-group
template). The schema framework is in [`schema-atlas.md`](schema-atlas.md); the physical
placement of each INTC instance in [`physical-intc-instances.md`](physical-intc-instances.md);
the APB address chain in
[`../../dma/sdma-windows-apb.md`](../../dma/sdma-windows-apb.md).

---

## 1. Top-line facts

| fact | value | evidence |
|---|---|---|
| trigger count | **243** | `yq 'length'` == 243 |
| top-level YAML type | flat sequence (`!!seq`) of mappings | `yq '.[0]\|type'` |
| keys per entry | **exactly 5** on all 243 | `[.[]\|keys\|length]\|unique` == `[5]` |
| key set | `{trigger, name, edge_triggered, needs_cdc, description}` | `[.[]\|keys[]]\|unique` |
| `edge_triggered:true` | **226** | group-by count |
| `edge_triggered:false` (level) | **17** | group-by count |
| `needs_cdc` | **false on all 243** | group-by count |
| missing/empty `trigger`/`name`/`description` | **0** | empties scan |
| duplicate triggers / names | **0** / **0** | `unique\|length` == 243 |

All counts in this page are re-grounded directly against the YAML (`yq`/`rg -c`),
never against a decompile grep.

> **NOTE — schema absence is itself a fact.** The keys `source_clock`,
> `source_reset_n`, and `msix_mask` are **absent on all 243 entries** (0/243). A
> reimplementer must not infer clock domain, reset, or MSI-X mask state from this
> artifact; they are not encoded here. This is the same 5-key schema as
> `sdma_triggers.yaml` (see [`schema-atlas.md`](schema-atlas.md) and
> [`sdma-triggers.md`](sdma-triggers.md)).

### 1.1 Per-entry schema

Every entry is a 5-key mapping. Verbatim from `intc_notific_intr[0]`:

```yaml
- trigger: intc_notific_intr[0]              # the HW signal name (may carry [N] / [N][M] index)
  name: notific_intr_0                       # the de-indexed legal identifier (brackets→underscore)
  description: An instruction notification…  # free-text cause
  needs_cdc: false                           # clock-domain-crossing flag — false on all 243
  edge_triggered: true                       # edge (226) vs level (17)
```

- **`trigger`** is the raw wire signal, retaining bus brackets (`intc_top_retrigger[7]`,
  `fis_sprot_intr[0][4]`). **`name`** is the normalized identifier the generator emits
  (`intc_top_retrigger_[7]`, `fis_sprot_intr_0_4`) — for some groups `name` keeps the
  raw `[N]` brackets and for others it normalizes them (see §3 QUIRK A2).
- **`edge_triggered`** distinguishes a one-shot edge latch from a level/condition input
  that the INTC samples while asserted.
- **`needs_cdc:false`** everywhere is consistent with every IO-fabric source being
  synchronous to the io-fabric/APB INTC clock domain. (data HIGH · OBSERVED; the
  synchronous-domain interpretation MED · INFERRED)

---

## 2. The 17 level-triggered (`edge_triggered:false`) sources

Everything else (the 226 retrigger / notific / errtrig / sprot / cntrl / pos_wr_nacc
sources) is edge. The level set is exactly:

```
intr_bvalid_id_not_expected   intr_bvalid_id_not_free   intr_bvalid_not_empty
intr_linked_list_overflow     intr_bvalid_err           intr_wstrb_err
intr_spurious_resp            intr_spare[0]             intr_spare[1]
se0_cfgbus_master_0_timeout   se0_cfgbus_master_1_timeout
se1_cfgbus_master_0_timeout   se1_cfgbus_master_1_timeout
iofab_axi2apb_0_timeout_type_valid_in   iofab_axi2apb_1_timeout_type_valid_in
io_fabric_apbblk_list_intr_r[0]         io_fabric_apbblk_list_intr_r[1]
```

Pattern: the AXI write-response integrity errors (`intr_bvalid*` / `linked_list` /
`wstrb` / `spurious`), the `*_timeout` watchdog outputs, the apbblk CAM-hit lines, and
the spares are level; everything that represents a discrete posted-write / notification
event is edge. (data HIGH · OBSERVED; pattern MED · INFERRED)

---

## 3. Sub-block grouping

Six comment banners partition the file (line numbers from the raw YAML):

| line | banner | block |
|---|---|---|
| L3 | `# 128b retrigger` | GROUP A |
| L645 | `# Abort signals from blocks` | **fully commented out — see QUIRK A1** |
| L660 | `# Notific intr` | GROUP B (indices 9..24) |
| L857 | `# sengine interrupt triggers` | GROUP E |
| L942 | `# fis cntrl interrupts, multi-bit vector from FIS CNTRL` | GROUP F |
| L1056 | `# All available FIS_ERRRIG triggers` | GROUP H |

Eight distinct base signals plus the IO-fabric singletons. Per-group counts (re-derived
with `yq '[.[].trigger]' \| jq -r '.[]' \| rg -o '^[a-z0-9_]+' \| sort \| uniq -c`),
summing to **243**:

| group | base signal | count | edge | level | sub-block / CSR family |
|---|---|---:|---:|---:|---|
| A | `intc_top_retrigger[0..127]` | **128** | 128 | 0 | INTC mailbox retrigger (`intc`) |
| B | `intc_notific_intr[0..24]` | **25** | 25 | 0 | NOTIFIC queue (`notific`) |
| C | `intr_bvalid_id_not_expected` | 1 | 0 | 1 | io-fabric AXI2APB write-tracker (`iofabric`) |
| C | `intr_bvalid_id_not_free` | 1 | 0 | 1 | ″ |
| C | `intr_bvalid_not_empty` | 1 | 0 | 1 | ″ |
| C | `intr_linked_list_overflow` | 1 | 0 | 1 | ″ |
| C | `intr_bvalid_err` | 1 | 0 | 1 | ″ |
| C | `intr_wstrb_err` | 1 | 0 | 1 | ″ |
| C | `intr_spurious_resp` | 1 | 0 | 1 | ″ |
| D | `intr_spare[0..1]` | **2** | 0 | 2 | reserved (no CSR) |
| E | `se0_cfgbus_master_{0,1}_{timeout,pos_wr_nacc_intr}` | 4 | 2 | 2 | sengine APB cfg-bus master |
| E | `se1_cfgbus_master_{0,1}_{timeout,pos_wr_nacc_intr}` | 4 | 2 | 2 | sengine APB cfg-bus master |
| E | `iofab_axi2apb_{0,1}_timeout_type_valid_in` / `_pos_wr_nacc_intr` | 4 | 2 | 2 | io-fabric AXI2APB (`iofabric`) |
| E | `io_fabric_apbblk_list_intr_r[0..1]` | 2 | 0 | 2 | apbblk CAM block-list (`apbblk`) |
| F | `fis_cntrl_intr[0..4]` | **5** | 5 | 0 | FIS control / APB-decode (`fis`) |
| G | `fis_sprot_intr[0..1][0..5]` | **12** | 12 | 0 | FIS sprot security/QoS (`sprot`) |
| H | `fis_errtrig_intr[0..49]` | **50** | 50 | 0 | FIS errtrig NOTIFIC (`notific`/`intc`) |
| | **TOTAL** | **243** | **226** | **17** | |

The 14-bucket sum (`128 + 25 + 4 + 1 + 1 + 1 + 2 + 8 + 4 + 2 + 5 + 12 + 50` where the
GROUP-C seven are split as `intr_bvalid×4 / linked_list / wstrb / spurious`) is **243**,
matching `yq 'length'` exactly.

### GROUP A — `intc_top_retrigger[0..127]` (128, all edge)

The 128-bit AXI-write retrigger vector. All 128 entries share one description:
*"re-triggers from axi-write to trigger mail box"* — an AXI write into the INTC mailbox
that re-asserts a trigger. This is the single largest group (53% of the file). Source
capacity is consistent with the `intc_1grp` INTC at `INTC_NUM_GROUPS×32` — the 128
retrigger lines occupy a full 4-group's worth of vector width even on a 1-group instance
(see [`../csr/intc-1group-apintc.md`](../csr/intc-1group-apintc.md) §3a).

### GROUP B — `intc_notific_intr[0..24]` (25, all edge)

One base signal physically split across two file regions:

- **`[9..24]`** sit under the `# Notific intr` banner (L660). `[9..16]` =
  *"notification request was dropped by wr_bufffer N"* (N=0..7); `[17..24]` =
  *"write buffer N is full"* (N=0..7).
- **`[0..8]`** appear **later** (≈L720–L845) with the rich nine-condition NOTIFIC cause
  set: SW NQ full, disabled NQ, HW-buffer-full stall, HW-buffer-full drop, AXI
  write-response error, outstanding-write stall, SW-queue threshold, NQ overlap,
  coalescer multi-stream hit. These nine match the NOTIFIC `NOTIFIC_*_BIT` map verbatim
  (see [`../csr/notific-queue.md`](../csr/notific-queue.md) §6.2).

### GROUP C — io-fabric AXI write-tracker integrity (7, all level)

`intr_bvalid_id_not_expected / _id_not_free / _not_empty`, `intr_linked_list_overflow`,
`intr_bvalid_err`, `intr_wstrb_err`, `intr_spurious_resp`. Bvalid/linked-list integrity
of the io-fabric AXI→APB write path.

### GROUP E — sengine + io-fabric AXI2APB + apbblk (14, mixed)

`se{0,1}_cfgbus_master_{0,1}` × {`timeout` (level), `pos_wr_nacc_intr` (edge)} = 8;
`iofab_axi2apb_{0,1}` × {`timeout_type_valid_in` (level), `pos_wr_nacc_intr` (edge)} = 4;
`io_fabric_apbblk_list_intr_r[0..1]` (level) = 2.

### GROUP F — `fis_cntrl_intr[0..4]` (5, all edge)

The FIS control multi-bit vector: AMZN-chain {AMZN, USER, USER-FIS} EP posted-write
slave errors (`[0..2]`) and USER-chain {USER, USER-FIS} EP slave errors (`[3..4]`).

### GROUP G — `fis_sprot_intr[0..1][0..5]` (12, all edge)

Two sprot instances × 6 bits. Per-instance bit map (identical for `[0]` and `[1]`):

| bit | meaning | CSR (§4) |
|---|---|---|
| 0 | `amzn_remapper` denied a transaction | `sprot/amzn_remapper.json` |
| 1 | delta monitor: R responses > AR requests | `sprot/amzn_remapper.json` `delta_mon` |
| 2 | tmu detected an AXI timeout | `sprot/amzn_remapper.json` `axi_{rd,wr}_timeout` |
| 3 | delta monitor: B responses > AW requests | `sprot/amzn_remapper.json` `delta_mon` |
| 4 | qos pmu (OR of all 16 PMU counter interrupts) | `sprot/qos_pmu.json` |
| 5 | spare (`fis_sprot_spare_0`) | — |

### GROUP H — `fis_errtrig_intr[0..49]` (50, all edge)

Two 25-entry banks under the `# All available FIS_ERRRIG triggers` banner:
- **`[0..24]`** = `user_errtrig` NOTIFIC — `wr_buffer[0..7]` full (8), `wr_buffer[0..7]`
  drop (8), then the nine NQ/AXI/threshold/overlap/coalescer conditions (9).
- **`[25..49]`** = `amzn_errtrig` NOTIFIC — byte-identical 25-condition pattern, only the
  `user_errtrig`→`amzn_errtrig` description prefix differs.

The `[16..24]` user-bank condition descriptions are byte-identical to GROUP B
`intc_notific_intr[0..8]` (same nine NOTIFIC causes). See
[`../csr/fis-errtrig-spad.md`](../csr/fis-errtrig-spad.md) §3.4 for the FIS errtrig SPAD
detail.

---

## 4. CSR-binding cross-checks (HIGH · OBSERVED unless noted)

Each trigger group is bound to a real CSR cause bit in
`csrs/{iofabric,fis,apbblk,notific,intc,sprot}/*.json`, confirmed two ways: (a) by direct
register/field-name match in the JSON, and (b) by the block-instance→JSON map in
`output/address_map/address_map_json_xref.yaml`. Register names quoted below are verbatim
from the JSON (`jq -r '..\|objects\|.Name?'`); register fields are nested under
`RegFile.RegistersBundleArrays[].Registers[].BitFields[]`.

### 4.1 GROUP A retrigger → `intc` mailbox

`intc_top_retrigger` is an AXI write into the INTC mailbox that re-asserts a vector bit;
the per-group INTC unit is `csrs/intc/intc_4grp_msix_unit.json` (template; the io-fabric
placement is the 1-group rendering `intc_1grp_msix_unit.json`). The xref confirms the
aggregator: `apb_io_0_user_io_intc_rdm_msix: csrs/intc/intc_1grp_msix_unit.json`.
(MED · INFERRED for the literal "retrigger" register — the mailbox write path is named
in the address map, not as a discrete named register in `iofabric_model`.)

### 4.2 GROUP B / GROUP H notific → `notific`

xref: `apb_io_0_user_io_intc_rdm_notific: csrs/notific/notific_10_queue.json` and
`apb_io_0_user_fis_io_intc_rdm_fis_0_user_errtrig_notific: csrs/notific/notific_1_queue.json`.
The `notific_10_queue.json` schema (bundles `notific` @0x0000, `notific_nq` @0x100; 47
registers) carries `sw_backpressure` (`on` @`NUM_SW_Q-1:0`), `hw_backpressure_lo/hi`
(`on` @31:0), `nq_full` (field `status` @`NUM_SW_Q-1:0`), `nq_threshold_en` /
`nq_threshold_passed`, `niq_buffer_0..4`, `naq_buffer_0..6`, and the `coal_ctrl`
coalescer (`force_use_coal` = *"Override Coalescer disable (due to SW NQ overlapping)"*) —
matching the GROUP B / GROUP H cause descriptions. The errtrig **trigger** routing is
`…_user_errtrig_trig_{0,1}: csrs/intc/intc_4grp_msix_unit.json` (the INTC vector inputs).

> **GOTCHA — `nq_full` is a field, `wr_buffer`/`drop` are description-only.** There is no
> `nq_full_status` register — the status is the field `nq_full.status`. The YAML cause
> phrases `wr_buffer`, `drop`, `overlap`, `coalescer` do **not** exist as register or
> field names; they appear only inside descriptions (`coal_ctrl.force_use_coal`'s text
> for overlap; the enable register pair is named `wr_buf_enable_lo/hi`, not `wr_buffer`).
> Bind the trigger by NOTIFIC condition bit, not by string-matching the YAML cause to a
> register name.

> **NOTE — `notific_10` vs `notific_1` are the same 47-register schema.** Both files have
> identical register and field NAMES; the only difference is the `Parameters` block:
> `NUM_SW_Q` = 10 vs 1 (and `SW_Q_RESET_TO_ALL_1` = 1023 vs 1), which sizes the
> `notific_nq` array and the `@NUM_SW_Q-1:0` fields. The IO-fabric top NOTIFIC uses the
> `_10_` variant; the per-FIS errtrig NOTIFIC uses the `_1_` variant. (See
> [`../csr/notific-queue.md`](../csr/notific-queue.md) §8.)

### 4.3 GROUP C / GROUP E AXI2APB + apbblk → `iofabric` / `apbblk`

- `iofab_axi2apb_{0,1}_timeout_type_valid_in` and `_pos_wr_nacc_intr` bind to
  `csrs/iofabric/iofabric_model.json`, bundle **`iofab`**. The `axi2apb_0` quad is:
  `axi2apb_0_timeout` [@0x10] (field `limit` @31:1 = *"number of 2-cycle periods to wait
  for an APB transaction to complete before timing out"*, reset `0xafc8`),
  `axi2apb_0_timeout_status` [@0x18] (`address` @31:0, *"Latch the address that triggered
  an error"*), `axi2apb_0_timeout_type` [@0x1c] (`write` @0, `valid` @1, *"latch the Type
  of transaction"*), and `axi2apb_0_timeout_ctrl` [@0x20] whose description is verbatim
  **"A write to this register clears the timeout interrupt"** (field `clear` @0). The
  `axi2apb_1` quad mirrors at @0x24–0x34. The `timeout_type.valid` bit @1 is the
  `..._timeout_type_valid_in` trigger source.
- `io_fabric_apbblk_list_intr_r[0..1]` bind to `csrs/apbblk/apbblk.json` (bundles
  `apbblk_ctrl` @0x0, `apbblk_cam_ctrl` @0x100, `apbblk_cam` @0x400). The interrupt fires
  on a CAM block-list hit, gated per-CAM by `apbblk_cam_ctrl.cam_ctrl_grp.intr_en_0..7`
  (*"Enable interrupt on blocked access to CAM_n address"*) and globally by
  `apbblk_ctrl.intr_on_unaligned`. Hit bookkeeping: `blocklist_version`, `num_cam_entries`,
  `blocked_response` (`blocked_{read,write}_pslverr`), `last_blocked_{read,write}_addr`
  (`address` @31:0), the `blocked/allowed_{read,write}_count_{lo,hi}` (+`_shadow`)
  counters, and the CAM `address`/`mask` (`val` @31:0). xref:
  `peb_apb_io_{0,1}_amzn_io_fabric_blk_{0,1}: csrs/apbblk/apbblk.json` plus the
  per-sengine `…_amzn_se_{0,1}_fabric_blk_{0,1}`.
- GROUP C `intr_bvalid*` / `linked_list` / `wstrb` / `spurious` belong to the same
  io-fabric AXI→APB write path but are **not** exposed as named registers in
  `iofabric_model.json` (no `bvalid`/`linked_list`/`wstrb`/`spurious` register, field, or
  description match anywhere in the file). The closest named status is the
  `axi2apb_0_ctrl` error-enable cluster (`rd_slv_err_en`, `wr_slv_err_en`, `rd_dec_err_en`,
  `wr_dec_err_en`). (HIGH that they belong to the AXI2APB write path · LOW for a precise
  register.)

> **GOTCHA — `se{0,1}_cfgbus_master_*` has no dedicated CSR JSON.** The string
> `cfgbus` appears only in the trigger YAMLs; no `papb_cfgbus_master` register-description
> file exists in this tree. Functionally these are identical to the `iofab_axi2apb`
> timeout/pos-wr-nacc pair (an APB cfg-bus master per sengine), so a reimplementer should
> model them on the `iofab` bundle. (HIGH for the role · LOW for a discrete register.)

### 4.4 GROUP F fis_cntrl → `fis`

`csrs/fis/fis_control.json` (bundles `desc`, `axi`, `apb`, `apb_decode`, `sw_cntrl`,
`iso_cntrl`, `apb_timeout`). The chain decode is `sw_cntrl.apb_amzn_decode`
(*"APB AMZN chain decode enable bits"*: `user1_en` @0, `user2_en` @8) and
`sw_cntrl.apb_user_decode` (`user1_en`/`user2_en`/`user_fis_en`/`user_debug_en`); the EP
watchdog is `apb_timeout.ctrl` (*"AMZN Chain EPs APB Timeout"*, field `limit` @31:0,
*"0=Disabled"*). The chain/EP decode + APB timeout raises the `fis_cntrl_intr[0..4]`
posted-write slave errors. `apb_decode.user_fis_block_id_override` (`new_block_id` @9:1,
`en` @0) ties to the USER-FIS EP path. (See
[`../csr/fis-errtrig-spad.md`](../csr/fis-errtrig-spad.md) §2.8.)

### 4.5 GROUP G fis_sprot → `sprot`

| bit | CSR file · bundle | register / field evidence |
|---|---|---|
| 0 | `amzn_remapper.json` · `control` | `addr_denied_lo` (`addr` @31:0, *"Denied Address [31:0]"*), `addr_denied_hi` (@25:0); `{amzn,user}_cam_stats.{rd,wr}_deny_cntr_{lo,hi}` |
| 1,3 | `amzn_remapper.json` · `delta_mon` | the delta-monitor bundle (R>AR / B>AW response-vs-request deltas) |
| 2 | `amzn_remapper.json` · `control` | `axi_rd_timeout`, `axi_wr_timeout` (the tmu AXI timeout) |
| 4 | `qos_pmu.json` · `csr` | `qos_pmu_intr_sts` = *"Latch which of the 16 PMU counter asserted an interrupt"* (`val` @15:0), `qos_pmu_intr_clr` |

The trigger-response controls that arm bits 1/3 live in `qos_prot.json` `csr` bundle:
each of the five `{ar,r,aw,w,b}_stall` registers carries `trigger_on_rresp` @24,
`trigger_on_bresp` @25, `trigger_on_wlast` @26, `retrigger` @29, `retrigger_max` @30; the
`nts_isolation.ctrl` adds `rd_timeout_en` @2 / `wr_timeout_en` @3.

xref confirms each FIS sub-block: `…_sprot_amzn_remapper → amzn_remapper.json`,
`…_sprot_qos → qos_prot.json` (per-SDMA path) or `qos_host_visible.json` (the IO-fabric
read-only monitor view), `…_sprot_user_remapper → user_remapper.json`,
`…_sprot_qos_pmu → qos_pmu.json`.

> **CORRECTION — the delta-monitor / tmu register family.** An earlier attribution
> placed the bit-1/2/3
> delta-monitor / tmu sources in `qos_prot.json`. Direct JSON inspection refines this:
> the **delta-monitor** bundle (`delta_mon`) and the **AXI timeout** counters
> (`axi_rd_timeout`/`axi_wr_timeout`) live in **`amzn_remapper.json`**; `qos_prot.json`
> holds only the trigger *arming* controls (`trigger_on_{bresp,rresp}`, `*_timeout_en` in
> `nts_isolation`) and the LFSR traffic-shaper — there is no `delta`/`tmu` register name
> in `qos_prot.json`. Both files are part of the sprot chain, so the routing claim stands;
> the register-family attribution is corrected here.

> **QUIRK — qos_pmu says "16" but defines 8 counter blocks.** `qos_pmu_intr_sts.val` is
> `@15:0` (*"Interrupt status for each of 16 PMU Counters"*), yet only eight physical
> counter blocks (`pmu_counter0..7`, each with `_event_select` / `_threshold_lo,hi` /
> `_cmp` / `_snap0_lo,hi` / `_snap1_lo,hi`) plus four AXI txn matchers
> (`axi_txn_matcher0..3`) are defined. The bit-4 trigger description's "16 PMU counter"
> phrasing matches the register width, not the populated-counter count. A reimplementer
> sizing the PMU should provision 8 live counters under a 16-bit status word. (HIGH ·
> OBSERVED)

---

## 5. Routing into the INTC

The IO-fabric sources collect into the **`io_intc_rdm`** INTC aggregator — the io-fabric
analogue of SDMA's `udma_gen`-style INTC. The xref shows the placement explicitly:

```
apb_io_0_user_io_intc_rdm_msix:    csrs/intc/intc_1grp_msix_unit.json   # the MSI-X vector
apb_io_0_user_io_intc_rdm_notific: csrs/notific/notific_10_queue.json   # the NOTIFIC queue
apb_io_0_amzn_io_intc_rdm_*  / apb_io_1_*  …                            # AMZN + io_1 siblings
```

and `apb_chain_defs.vh` places the io-fabric + INTC bases on one APB chain:

```
`APB_IO_0_AMZN_IO_FABRIC_BASE        # the io_fabric block (AXI2APB / apbblk sources)
`APB_IO_0_AMZN_IO_INTC_RDM_BASE      # the AMZN INTC aggregator
`APB_IO_0_USER_IO_INTC_RDM_BASE      # the USER INTC aggregator
`APB_IO_0_AMZN_PEB_USER_FIS_0_SPROT_BASE   # fis_sprot_intr[0]
`APB_IO_0_AMZN_PEB_AMZN_FIS_0_SPROT_BASE   # fis_sprot_intr[1]
```

So the two `fis_sprot_intr` indices `[0]`/`[1]` correspond to the **USER-FIS** and
**AMZN-FIS** sprot instances on the io_0 APB chain (and mirror on io_1). The notific and
errtrig sources pass through NOTIFIC queue CSRs first; the sprot/QoS sources through the
`sprot` family; the AXI2APB / apbblk sources through `iofabric_model` / `apbblk` — then
all converge as vector-bit inputs to the `io_intc_rdm` `intc_1grp_msix_unit`. (HIGH for
the aggregation point · MED for the exact per-trigger wire into a specific vector bit.)

### 5.1 Routing-count reconciliation vs `#923`

These counts are held identical to the io-fabric APB INTC trigger inventory established by
[`../csr/intc-1group-apintc.md`](../csr/intc-1group-apintc.md) (L398–405) and
[`../csr/intc-4group.md`](../csr/intc-4group.md) (L475, `io_fabric_triggers | 243`):

| source | count | this page | `#923` | status |
|---|---:|:---:|:---:|---|
| `intc_top_retrigger[0..127]` | 128 | ✓ | ✓ | consistent |
| `fis_errtrig_intr[*]` | 50 | ✓ | ✓ | consistent |
| `intc_notific_intr[*]` | 25 | ✓ | ✓ | consistent |
| `fis_sprot_intr[*]` | 12 | ✓ | ✓ | consistent |
| `fis_cntrl_intr[*]` | 5 | ✓ | ✓ | consistent |
| total | **243** | ✓ | ✓ | consistent |

No CORRECTION is raised on the routing counts: this page's per-group counts agree exactly
with the committed CSR siblings (`#922`/`#923`/`#918`). (The only correction in this page
is the GROUP-G register-family attribution, §4.5.)

---

## 6. Anomalies and quirks

> **QUIRK A1 — the `# Abort signals from blocks` block is commented out (L645–L658).**
> It is **not** part of the 243 (every line begins with `#`). It is a Mako generator
> (`#% for bit in range(62):`) that would emit `pmdtu_to_cctm[127].local_abort` /
> `block_abort_127` lines plus a `pmuc_interrupt` (*"CCTM signalled interrupt"*). These
> CCTM / block-abort sources are **disabled** in this artifact — present in the
> generator template but excluded from the active set. A reimplementer targeting CCTM
> aborts must re-enable them; the Cayman io-fabric INTC does not carry them.

> **QUIRK A2 — two naming conventions for one base signal.** `intc_notific_intr[0..8]`
> use clean names (`notific_intr_N`); `intc_notific_intr[9..24]` keep the raw bracket
> (`intc_notific_intr_[N]`). Same base signal, inconsistent `name` normalization.

> **QUIRK A3 — non-monotonic index order.** `intc_notific_intr` appears as `[9..24]`
> first (under the banner) and `[0..8]` later in the file. Do not assume file order ==
> index order when parsing.

> **GOTCHA A4 — preserved description typos.** `wr_bufffer` (triple-f) appears **8×**
> (`intc_notific_intr[9..16]`); `interrrupt` (triple-r) appears **2×**
> (`fis_sprot_intr[0][4]`, `fis_sprot_intr[1][4]`, both *"OR of all 16 PMU counter
> interrrupts"*). Verbatim in the source; match them exactly if string-comparing.

> **NOTE A5 — `se1` timeout descriptions omit the master index.**
> `se1_cfgbus_master_{0,1}_timeout` both read *"se1 papb_cfgbus_master timeout interrupt"*
> (no `0`/`1`), whereas the `se0` pair and the `se1` `pos_wr_nacc` entries correctly
> embed `master0`/`master1`. The `trigger` field still distinguishes them; only the
> description is index-less.

> **NOTE A6 — interleaved GROUP C.** `intr_linked_list_overflow` is placed **between**
> `intr_bvalid_not_empty` and `intr_bvalid_err`, splitting the bvalid set in file order.

> **NOTE A7 — duplicate-description families (by design).** All 128 retrigger entries
> share one description; `intc_notific_intr[0..8]` descriptions are byte-identical to
> `fis_errtrig_intr[16..24]` and `[41..49]`; the errtrig user-bank `[0..24]` mirrors the
> amzn-bank `[25..49]` verbatim except the `user_errtrig`/`amzn_errtrig` prefix.

> **NOTE A8 — no version gate.** Unlike `sdma_triggers.yaml`, `io_fabric_triggers.yaml`
> carries no `(V4)`-style version-gate marker anywhere; the entire 243-entry set is
> unconditional in this artifact.

---

## 7. v5 / MAVERICK divergence (OBSERVED — see callout)

A second `io_fabric_triggers.yaml` ships under the MAVERICK (v5) arch-header tree
(`arch-headers/maverick/intc/io_fabric_triggers.yaml`, 1298 lines). It is a **full**
file (not a header stub), so its interior is directly observable.

> **CORRECTION / DIVERGENCE (Cayman vs MAVERICK v5).** The v5 file carries **241**
> triggers, not 243. Two structural changes vs Cayman: (1) GROUP F
> `fis_cntrl_intr[0..4]` (5 entries, the multi-bit vector) is **collapsed to a single
> scalar** `fis_cntrl_intr`; (2) two **new** FIS parity sources are added —
> `parity_addr_error_interrupt` (*"Address Parity Error from FIS"*) and
> `parity_data_error_interrupt` (*"Data Parity Error from FIS"*), both edge,
> `needs_cdc:false`. Net: 243 − 5 + 1 + 2 = **241** (`yq 'length'` == 241, edge 224 /
> level 17). GROUPS A/B/C/D/E/G/H are unchanged (128/25/7/2/14/12/50). This matches the
> architectural note that the Cayman HW-only `fis_cntrl_intr[0..4]` triggers were promoted
> into a first-class bundle (with an AXI-parity addition) in the Mariana/MAVERICK
> generation — see [`../csr/fis-errtrig-spad.md`](../csr/fis-errtrig-spad.md) §5. All v5
> facts in this section are **OBSERVED** from the v5 YAML bytes; the v5-interior of any
> family not enumerated above remains **INFERRED** to match Cayman. (HIGH · OBSERVED for
> the three changed entries; MED · INFERRED for unenumerated v5 interior parity.)

The Cayman 243-entry map remains the reference for the v3-class (Cayman) io-fabric control
plane described throughout this guide.

---

## 8. Provenance

| input | role |
|---|---|
| `intc/io_fabric_triggers.yaml` (43955 B, 1310 lines, mtime Aug 8 2023) | the 243-entry trigger sequence |
| `csrs/{iofabric,fis,apbblk,notific,intc,sprot}/*.json` | per-group CSR cause-bit cross-check |
| `output/address_map/address_map_json_xref.yaml` | block-instance → JSON map (routing) |
| `output/address_map/apb_chain_defs.vh` | the APB address chain (io_fabric + INTC + sprot bases) |
| `arch-headers/maverick/intc/io_fabric_triggers.yaml` (1298 lines) | v5 divergence (§7) |

All facts are derived from static analysis of the shipped register/trigger description
artifacts; recovered symbols, strings, JSON and YAML are binary-derived and citeable.
Counts re-grounded with `yq`/`jq`/`rg -c` against the artifacts themselves.
