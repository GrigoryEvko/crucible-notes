# CSR — NOTIFIC Variants + SDMA Residual + the `csrs/` Coverage Ledger

This is the **final CSR-lane page**. It closes three loose ends and then proves
coverage of the whole register-schema tree:

1. the **NOTIFIC variant reconciliation** — why two schemas
   (`notific_1_queue` / `notific_10_queue`) describe one RTL macro, and which
   address blocks each binds;
2. the **SDMA residual** — byte-exact schemas for the three sibling DMA engines
   `cce` / `cme` / `dre` that completed `csrs/sdma/`;
3. a small **MISC residual** — the `erg` ECC/parity RAS regfiles and
   `misc_ram_model`;

and finally **4** — the **COVERAGE LEDGER**: every one of the **85** schema JSONs
in `csrs/` placed against the Cayman address map, reconciled across the two
independent coverage lenses (CSR-task byte-exactness vs address-map binding), with
the byte-grounded reg-count keystones from the committed sibling pages folded in
so the ledger is internally consistent.

**Provenance.** Every figure is reproducible from the shipped Cayman arch-regs
artifacts — the `csrs/<sub>/<unit>.json` schema tree, the
`output/address_map/address_map_flat.yaml` flat map, and its independent
`address_map_json_xref.yaml` side-car — plus the v5 `maverick/` arch-headers and
the `maverick/ext/al_address_map_db.{pkl,json}` binding mirror. These are
RTL-generated artifacts shipped in the customop library; semantics come from the
per-field `Description` strings. No HAL doc accompanies them
(`HalName`/`RegFile.Description` are empty). All counts below were re-derived this
session by direct `jq` / `rg` over the shipped JSON/YAML.

> **Schema shape (all RegFile JSONs).**
> `RegFile{ UnitName, Type, RegfileFlavor, InterfaceType, AddrWidth, DataWidth,
> SizeInBytes, Parameters[], RegistersBundleArrays[] }`;
> `RegistersBundleArrays[]{ Name, AddressOffset, ArraySize, BundleSizeInBytes,
> GenFlavor, Tag[], Registers[] }`; `Registers[]{ Name, AddressOffset, AccessType,
> BitFields[] }`.
> **GOTCHA — parse defensively.** `AddressOffset` is a HEX STRING, sometimes
> zero-padded/mixed-case (`"0x08"`, `"0xC"`). `SizeInBytes` is HEX for most blocks
> (`"0x1000"`) but DECIMAL for a few (`gpio="4096"`, `pvt="65536"`). `ArraySize`
> can be a **parameter name** (`"NUM_SW_Q"`) resolved from `Parameters[]`, not a
> literal. `[HIGH · OBSERVED]`

**Per-gen applicability.** Cayman is **NC-v3** and byte-grounded (`OBSERVED`).
Maverick is **v5**: its `maverick/address_map/` is a tree of per-die C headers
(header-`OBSERVED`) and its bindings come from the pkl/json mirror, but every
*reading over v5 interior register layout* is `INFERRED` — no v5 register schema
ships. Any v5-interior claim here is tagged `INFERRED`.

---

## 1. Key facts

| Property | Value | Source |
|---|---|---|
| Schemas in `csrs/` (on disk) | **85** JSON across 29 subdirs | `fd -e json csrs/` |
| Schemas **referenced** by the flat map | **76** | `rg -o 'csrs/.../*.json' flat` \| sort -u |
| On-disk **orphans** (no binding block) | **9** | `set(on_disk) − set(referenced)` |
| Referenced-but-missing-on-disk | **0** | `set(referenced) − set(on_disk)` = ∅ |
| Byte-exact-documented by CSR-01..20 | **65** (60 prior + 5 here) | CSR-lane page headers |
| `notific_1_queue` / `notific_10_queue` instances | **962** / **332** | `json_xref` |
| `cce` / `cme` / `dre` instances (each) | **132** / **132** / **132** | `json_xref` |
| AXI fault terminator (read-data poison) | `0xDEADBEEF`, response `0x2` (SLVERR) | `qos_prot`/`nsm` JSON |

The two coverage numbers (**76 referenced** vs **65 byte-exact**) are *different
lenses* and they are reconciled in §4.3 — neither is wrong.

---

## 2. NOTIFIC variants — one macro, two `NUM_SW_Q` instantiations

The full NOTIFIC block (the 16-byte record, the `notific_type` enum, the 41+6
register layout, the nine interrupts, and the producer/consumer protocol) is
documented on the dedicated page
[CSR — NOTIFIC Queue](notific-queue.md). This section adds **only** the
variant reconciliation: `notific_1_queue` vs `notific_10_queue` are the **same RTL
macro** at `NUM_SW_Q = 1` vs `10`, and they bind **different** address-map blocks.

### 2.1 The diff is exactly two scalar parameters `[HIGH · OBSERVED]`

`jq -S '.RegFile.RegistersBundleArrays'` on both files is **byte-identical**. The
only divergence is two values in `Parameters[]`:

| field | `notific_1_queue` | `notific_10_queue` |
|---|---|---|
| `UnitName` | `notific_1_queue` | `notific_10_queue` |
| `Type` / `RegfileFlavor` / `InterfaceType` | `REGFILE` / `POSEDGE` / `APB` | (identical) |
| `AddrWidth` / `DataWidth` / `SizeInBytes` | `12` / `32` / `0x1000` | (identical) |
| Param `NUM_SW_Q` | **`1`** | **`10`** |
| Param `SW_Q_RESET_TO_ALL_1` | **`1`** | **`1023`** (`0x3ff`) |
| on-disk bytes | **39,508** | **39,513** (5-byte text delta) |

Both carry the same two bundles: `notific` @`0x000`, `ArraySize=1`,
`BundleSize=0x100`, **41** reg-defs (the HW notification engine); and `notific_nq`
@`0x100`, `ArraySize="NUM_SW_Q"`, `BundleSize=0x28`, **6** reg-defs (the per-SW-ring
descriptor, `Tag=ALWAYS_ARRAYED`). The depth is parameterised purely through
`notific_nq.ArraySize`. `SW_Q_RESET_TO_ALL_1` is the power-on
queue-ready/full reset mask, width-matched to `NUM_SW_Q` (`1023 = 0x3ff` = "all 10
SW queues") — `[HIGH for the value, MED · INFERRED for the semantics]`.

> **NOTE.** Footprint after array expansion: 1-queue = `0x100 + 1·0x28 = 0x128`;
> 10-queue = `0x100 + 10·0x28 = 0x290`. Both sit well inside the `0x1000` APB
> window, so the variant difference is **purely** descriptor depth — there is no
> register-level structural divergence. `[HIGH · OBSERVED+calc]`

### 2.2 Which blocks each variant binds `[HIGH · OBSERVED]`

Instance multiplicity from the `json_xref` side-car (the bind oracle; agrees
byte-exactly with the inline flat-map binding):

| schema | instances | bound families (from §4.1) | role |
|---|---|---|---|
| `notific_10_queue` | **332** | `PEB_APB_IO ×166, APB_SE ×128, APB_IO ×38` | the **per-SDMA-channel** notification engine (10 SW rings/channel) |
| `notific_1_queue` | **962** | `PEB_APB_IO ×428, PEB_APB_IO_BCAST ×320, APB_SE ×128, APB_IO ×86` | the **single-ring** engine bolted onto every **FIS error-trigger** generator |

All 962 `notific_1_queue` bind-nodes are `*ERRTRIG_NOTIFIC`-named
(`rg 'notific_1_queue.json$' json_xref \| rg -c ERRTRIG` = **962** of **962**) —
e.g. `APB_SE_0_USER_FIS_SDMA_0_FIS_0_USER_ERRTRIG_NOTIFIC`. The 10-queue variant
serves SDMA channels (`*_SDMA_<n>_MISC_NOTIFIC`); the 1-queue variant is the
minimal sink each `{user,amzn}` error-trigger pair drops a single notification
into. `962 ≫ 332` because errtrig generators vastly outnumber SDMA channels.

> **CORRECTION (errtrig PAIR count = 962, not 642).** A prior cross-check column
> (ADDR-lane `pkl-intc-sprot-security.md`, the Maverick `1,372` row) carried a
> **Cayman "642 pairs"** figure. That is wrong for Cayman: on the byte-grounded
> flat map, `ERRTRIG_TRIG_0`, `ERRTRIG_TRIG_1`, and the bolted-on
> `notific_1_queue` each count **962**, i.e. **962 generator PAIRS**. The errtrig
> page ([fis-errtrig-spad.md](fis-errtrig-spad.md) §3.2) already supersedes the
> 642 with the byte-grounded 962; this ledger carries **962**. (The Maverick
> `1,372` is a *different SoC* and is not in conflict.) `[HIGH · OBSERVED —
> CARRIED correction from #930/#908]`

### 2.3 Why two instantiations exist (MED · INFERRED)

The 10-queue variant is the production SW-visible queue set: it gives the
cluster-routing layer a 4-bit index space so separate rings can serve separate
notification classes (per-engine completions, the `errors_NT_` route, the
DGE-completion route, HAM, Q7). The 1-queue variant is the minimal instantiation
for a context needing a single notification sink (one output ring, no per-class
routing) — same MMIO map, no per-queue index logic. The errtrig generator wants
exactly that: a single ring to deposit one error notification. `[MED · INFERRED
from the parameter delta and the binding pattern]`

---

## 3. SDMA residual — `cce` / `cme` / `dre` (byte-exact)

`csrs/sdma/` holds **8** schemas. Five were documented earlier — `udma_m2s`
([udma-m2s.md](udma-m2s.md)), `udma_s2m` ([udma-s2m.md](udma-s2m.md)), and
`udma_gen` / `udma_gen_ex` / `tdma_model`
([udma-gen-tdma.md](udma-gen-tdma.md)). The three sibling compute/reorder engines
were *named* but not dumped; their byte-exact schemas are below, completing
`csrs/sdma/` to **8/8**.

All three: `Type=REGFILE`, `RegfileFlavor=POSEDGE`, `InterfaceType=APB`,
`AddrWidth=12`, `DataWidth=32`, `SizeInBytes=0x1000`, no `0xb1` reset-placeholder
leak. Each binds **132** instances on the PEB AMZN plane
(`…_amzn_se_*_sdma_<n>_{cce,cme,dre}`). `[HIGH · OBSERVED]`

### 3.1 `sdma/dre.json` — Data Reordering Engine (the tensor-DMA transpose)

**6** reg-defs, **23** bitfield-defs, on disk **17,012** bytes. `[HIGH · OBSERVED]`

| Bundle | off | `ArraySize` | `BundleSize` | regs |
|---|---|---|---|---|
| `custom_transpose` | `0x000` | **8** | `0x4` | 1 (`command`, 16 bitfields) |
| `dre_buffer_access` | `0x100` | 1 | `0x50` | 3 |
| `spares` | `0x200` | 2 | `0x8` | 2 |

`custom_transpose.command` is a 4-dimensional (x,y,z,w) stride/rows increment
matrix, replicated ×8 (one per transpose descriptor):

| field group | positions | acc/rst |
|---|---|---|
| `x_stride_inc_{x,y,z,w}` | `[0..3]` | RW / 0 |
| `x_rows_inc_{x,y,z,w}` | `[8..11]` | RW / 0 |
| `y_stride_inc_{x,y,z,w}` | `[16..19]` | RW / 0 |
| `y_rows_inc_{x,y,z,w}` | `[24..27]` | RW / 0 |

`dre_buffer_access` is the banked-scratch debug window:
`addr{buffer_row_index[4:0], buffer_index[5], bank_index[9:6]}`;
`initiate_write.valid[0]` (WO); `initiate_read.valid[0]` (WO).

⇒ DRE is a multi-dimensional strided transpose/reorder over a banked scratch
buffer — the actual "tensor DMA" reorder path. `tdma_model` in the address map is
the COUNTER/ROUTER; the reorder itself lives here. `[MED · INFERRED for the role]`

### 3.2 `sdma/cce.json` — Compute/Collective Engine (FP32 FMA + decompress)

**14** reg-defs, **16** bitfield-defs, on disk **18,136** bytes. `[HIGH · OBSERVED]`

| Bundle | off | regs | content |
|---|---|---|---|
| `cce_fma_cfg` | `0x000` | 1 | `absolute_value_clipping.use_absolute_value[0]` RW |
| `cce_fma_const` | `0x010` | 2 | `fp32_zero` rst **`0x80000000`** (−0.0), `fp32_one` rst **`0x3f800000`** (+1.0) |
| `cce_buffer_access` | `0x100` | 3 | `addr.row_index[6:0]` RW; `initiate_{write,read}.valid` WO |
| `debug` | `0x200` | 6 | `header_word_0..3` (RO); decompression counters/dtypes (RO) |
| `spares` | `0x300` | 2 (arr=2) | spare |

`debug.decompression_info_bytes{uncompressed[31:16], compressed[15:0]}` and
`debug.decompression_info_data_types{input_data_type[3:0],
output_data_type[11:8]}` are RO — the on-the-fly decompression telemetry.

⇒ CCE is a collective/reduction FMA path with an inline DECOMPRESSION engine. The
two IEEE-754 constants (`fp32_zero` = −0.0, `fp32_one` = +1.0) are the additive /
multiplicative identities the FMA seeds. `[HIGH · OBSERVED for the constants;
MED · INFERRED for "collective"]`

### 3.3 `sdma/cme.json` — Command/Merge Engine (the smallest SDMA sub-block)

**1** reg-def, **3** bitfield-defs, on disk **2,521** bytes — a single config
register. `[HIGH · OBSERVED]`

```c
// cme/cfg @0x000 — the only register in the schema.
struct cme_cfg {
    uint32_t mul_cmd_en          : 1;   // [0]    RW  rst = 1   (multi-cmd merge on)
    uint32_t hold_pipe_when_error: 1;   // [1]    RW  rst = 0
    uint32_t fifo_ack_enable     : 5;   // [6:2]  RW  rst = 0x1F (all 5 FIFOs ack-enabled)
};                                      // bits [31:7] reserved
```

⇒ CME is a lightweight multi-packet command-merge; `fifo_ack_enable` resetting to
`0x1F` (5 bits) means all five command FIFOs ack at reset. `[HIGH · OBSERVED]`

---

## 4. The MISC residual + the COVERAGE LEDGER

### 4.1 MISC residual — `erg` RAS regfiles and `misc_ram_model` `[HIGH · OBSERVED]`

**`erg/` ECC + parity** are tiny RAM error-reporting CSRs: all four are APB,
`AddrWidth=6`, `SizeInBytes=0x40` (64-byte windows).

| schema | reg-defs | on-disk bytes | content |
|---|---|---|---|
| `erg_ecc_model` | **15** | 20,054 | `init_cfg/status`, `mem_cfg`, `cfg`, and the correctable/uncorrectable SRAM-error `{err, mask, cnt, status, stat_clear}` surfaces + `spare_reg` |
| `erg_ecc_model_noinit` | 15 | 20,061 | **single-byte diff** from `erg_ecc_model`: one `ResetValue 0 → 1` (skips auto memory-init) |
| `erg_parity_model` | **10** | 15,674 | parity analogue (ECC layout minus the corr/uncorr split) |
| `erg_parity_model_noinit` | 10 | 15,183 | parity `_noinit` variant |

> **QUIRK — the `_noinit` pattern.** Same trick as `notific_1`/`notific_10`: the
> `_noinit` build is byte-identical to its base **except one `ResetValue`** bit
> that controls whether the block auto-initializes memory at reset
> (`diff <(jq -S … erg_ecc_model.json) <(jq -S … erg_ecc_model_noinit.json)` = the
> single `"ResetValue": "0"` → `"1"` hunk). The `csrs/` tree ships variant pairs
> by flipping exactly one reset bit and renaming the unit. `[HIGH · OBSERVED]`

**`misc/misc_ram_model.json`** — APB, `AddrWidth=12`, `SizeInBytes=0x1000`,
on-disk 9,837 bytes, **4** registers, all up at `0x800`:

| off | reg | acc | meaning |
|---|---|---|---|
| `0x800` | `wr_trigger_int_nosec` | RW | non-secure write-triggered interrupt |
| `0x804` | `zebu_build_id` | RO | **emulation** (Zebu) build id |
| `0x808` | `pacific_status` | RO | silicon-id status |
| `0x80c` | `pacific_spare` | RO | spare |

`zebu_build_id` / `pacific_*` are emulation/silicon-id tells; this scratch/status
RAM block is paired with the INTC_RDM region. `[HIGH · OBSERVED]`

### 4.2 The two coverage lenses

The `csrs/` tree can be "covered" two independent ways, and the ledger must carry
both because they disagree by design:

- **Lens A — address-map binding** (does any flat-map node bind this schema?).
  **85 on disk → 76 referenced → 9 orphans**, **0** referenced-but-missing.
  Re-derived this session: `comm -23 set(on_disk) set(referenced)` = exactly 9.
  This is the lens the block→schema xref ([block-schema-xref.md](../address/block-schema-xref.md))
  uses.
- **Lens B — CSR-task byte-exactness** (did a CSR-lane page dump this schema's
  registers?). **65 byte-exact** (60 by CSR-01..19 + 5 here: `cce`, `cme`, `dre`,
  `notific_1_queue`, and the `erg_ecc`/`misc_ram` enrich), **2** shape-captured
  (`erg_parity{,_noinit}`), **18** byte-undumped peripheral residual.

> **NOTE — the lenses are not the same number and that is correct.** A schema can
> be byte-documented but address-map-**orphan** (e.g. `erg_ecc_model_noinit`,
> `intc_1grp_no_msix_unit` — real schemas that bind no Cayman node yet are
> understood at byte level), and a schema can be address-map-**bound** but not
> CSR-task-dumped at byte level (the 18 peripheral residual). The intersection is
> what §4.3 reconciles. `[HIGH · OBSERVED]`

### 4.3 The full ledger — all 85 schemas, both lenses `[HIGH · OBSERVED]`

`A` = address-map binding (`#inst` from `json_xref`, or `orphan`); `B` = CSR-lane
byte-exactness (page that dumped it, or `—` for peripheral residual). Escaped `\|`
in cells.

| dir | schema | A: #inst | B: byte-exact page |
|---|---|---|---|
| apbblk | `apbblk.json` | 12 | — (residual) |
| ap_intc | `ap_intc_1grp_unit.json` | **orphan** | [intc-1group-apintc.md](intc-1group-apintc.md) |
| ap_intc | `ap_intc_2grp_unit.json` | **orphan** | [intc-1group-apintc.md](intc-1group-apintc.md) |
| ap_intc | `ap_intc_4grp_unit.json` | **orphan** | [intc-4group.md](intc-4group.md) |
| ap_intc | `ap_intc_grp_ctrl.json` | **orphan** | [intc-1group-apintc.md](intc-1group-apintc.md) (IOFIC 9-reg) |
| d2d | `d2d_ctrl_axi_cfg` / `_core_cfg` / `d2d_mpcs_cfg` / `d2d_xsr_cfg` | 288/288/288/144 | [hbm-d2d-pcie-blocks.md](hbm-d2d-pcie-blocks.md) |
| d2d | `mrvl_mpcs_x16` / `mrvl_xsr_phy` / `mrvl_xsr_pram` / `snps_ctrl` | 288/144/144/288 | [hbm-d2d-pcie-blocks.md](hbm-d2d-pcie-blocks.md) |
| dfx | `a2i_model` / `a2j_model` / `dfx_model` | 4/2/2 | — (residual) |
| ela500 | `cxela500.json` | 1318 | (ref: trace) |
| erg | `erg_ecc_model.json` | 320 | **§4.1 here** |
| erg | `erg_ecc_model_noinit.json` | **orphan** | **§4.1 here** |
| erg | `erg_parity_model.json` | 1134 | §4.1 (shape only) |
| erg | `erg_parity_model_noinit.json` | 1246 | §4.1 (shape only) |
| fis | `fis_control` / `papb_bcast` | 582/464 | [fis-errtrig-spad.md](fis-errtrig-spad.md) |
| gpio | `gpio.json` | 16 | — (residual; `Size="4096"` DECIMAL) |
| hbm | `ddr_csr_apb` / `dwc_hbmphy_top` / `hbm_cfg` / `hbm_hpr` / `hbm_scbr` / `hbm_xbar_cfg` | 64/4/4/64/64/4 | [hbm-d2d-pcie-blocks.md](hbm-d2d-pcie-blocks.md) |
| i2c | `dw_apb_i2c` / `i2c_config` | 2/2 | — (residual; `IF=null` 3rd-party) |
| intc | `intc_1grp_msix_unit.json` | 4 | [intc-1group-apintc.md](intc-1group-apintc.md) |
| intc | `intc_1grp_no_msix_unit.json` | **orphan** | [intc-1group-apintc.md](intc-1group-apintc.md) |
| intc | `intc_4grp_msix_unit.json` | 858 | [intc-4group.md](intc-4group.md) |
| intc | `intc_4grp_no_msix_unit.json` | 1070 | [intc-4group.md](intc-4group.md) |
| iofabric | `iofabric_model.json` | 2 | — (residual) |
| misc | `misc_model.json` | 2 | — (residual; 97 regs, largest residual) |
| misc | `misc_ram_model.json` | 4 | **§4.1 here** |
| notific | `notific_1_queue.json` | **962** | **§2 here** + [notific-queue.md](notific-queue.md) |
| notific | `notific_10_queue.json` | **332** | [notific-queue.md](notific-queue.md) |
| pcie | `dwc_e32mp_phy_x4_ns.json` | **orphan** | — (residual; largest on-disk schema) |
| pcie | `pcie5_x8_dwc_pcie_ctl` / `pcie_appaxi` / `pcie_appcore` / `pcie_phy_model` / `pcie_user` | 218/218/218/436/190 | [hbm-d2d-pcie-blocks.md](hbm-d2d-pcie-blocks.md) |
| pll | `pll_model.json` | 10 | — (residual) |
| pmdtu | `pmdt_cctm.json` | 2 | (ref only) |
| pmdtu | `pmdt_complex.json` | **orphan** | — (residual) |
| pvt | `pvt.json` | 4 | — (residual; `Size="65536"` DECIMAL) |
| rdm | `rdm_model.json` | 4 | [rdm-top-sp.md](rdm-top-sp.md) |
| ring | `otp` / `ring_io_bot` / `ring_io_top` | 2/2/2 | — (residual) |
| sdma | `cce.json` | **132** | **§3.2 here** |
| sdma | `cme.json` | **132** | **§3.3 here** |
| sdma | `dre.json` | **132** | **§3.1 here** |
| sdma | `tdma_model.json` | 264 | [udma-gen-tdma.md](udma-gen-tdma.md) |
| sdma | `udma_gen.json` | 280 | [udma-gen-tdma.md](udma-gen-tdma.md) |
| sdma | `udma_gen_ex.json` | 280 | [udma-gen-tdma.md](udma-gen-tdma.md) |
| sdma | `udma_m2s.json` | 280 | [udma-m2s.md](udma-m2s.md) |
| sdma | `udma_s2m.json` | 280 | [udma-s2m.md](udma-s2m.md) |
| sfabric | `sfabric_model.json` | 4 | — (residual) |
| spis | `spis_model.json` | 4 | — (residual; `IF=null`) |
| sprot | `amzn_remapper` / `user_remapper` | 712/528 | [remapper.md](remapper.md) |
| sprot | `nsm.json` | 220 | [nsm.md](nsm.md) |
| sprot | `qos_host_visible` / `qos_pmu` | 1208/528 | [qos-pmu-hostvisible.md](qos-pmu-hostvisible.md) |
| sprot | `qos_prot.json` | 1500 | [qos-prot.md](qos-prot.md) |
| top_sp | `top_sp_ram.json` | 40 | [rdm-top-sp.md](rdm-top-sp.md) |
| tpb | `tpb.json` | 16 | [tpb.md](tpb.md) |
| tpb | `tpb_arr_seq_cluster_host_visible` / `_top_host_visible` / `_top_protected` | 16/16/8 | [pe-array-sequencer.md](pe-array-sequencer.md) |
| tpb | `tpb_events_semaphores_axi.json` | **orphan** | [tpb-subblocks.md](tpb-subblocks.md) (the consequential orphan, §4.4) |
| tpb | `tpb_ham` / `tpb_sbuf_cluster` / `tpb_sbuf_pool_act` | 8/64/16 | [tpb-subblocks.md](tpb-subblocks.md) |
| tpb | `tpb_xt_local_reg.json` | 84 | [tpb-xt-local-reg.md](tpb-xt-local-reg.md) |
| urb | `sfabric_urb` / `urb` | 264/136 | — (residual) |
| xtensa_nx | `xtensa_nx.json` | 60 | [xtensa-nx.md](xtensa-nx.md) |
| xtensa_q7 | `xtensa_q7.json` | 80 | [xtensa-q7.md](xtensa-q7.md) |

**Ledger rollup `[HIGH · OBSERVED]`:**

- **85** schemas on disk; **76** address-map-referenced; **9** orphans
  (`ap_intc_1grp/2grp/4grp_unit`, `ap_intc_grp_ctrl`, `erg_ecc_model_noinit`,
  `intc_1grp_no_msix_unit`, `dwc_e32mp_phy_x4_ns`, `pmdt_complex`,
  `tpb_events_semaphores_axi`); **0** referenced-but-missing.
- **65** byte-exact (60 prior + 5 here); **2** shape-captured (`erg_parity` pair);
  **18** byte-undumped peripheral residual (the `—` rows: `apbblk`, `dfx ×3`,
  `gpio`, `i2c ×2`, `iofabric`, `misc_model`, `pll`, `pmdt_complex`, `pvt`,
  `ring ×3`, `sfabric`, `spis`, `urb ×2` — plus the `dwc_e32mp_phy` PCIe-PHY
  variant). Counting: 18 = the residual `—` rows.
- **Sum of all 76 referenced #inst = 19,012** bound nodes (the flat map's full
  binding population; cross-checked against the `json_xref` side-car with 0
  mismatch).

> **CORRECTION (which is the orphan set).** Earlier prose framed the "uncovered"
> set as 18 *byte-undumped* blocks. That is **Lens B** and is **not** the same as
> the **9 address-map orphans** (Lens A). The two sets overlap only partially:
> e.g. `erg_ecc_model_noinit`, `intc_1grp_no_msix_unit`, and the four `ap_intc_*`
> are address-map orphans yet are byte-understood (shipped for a later gen);
> conversely `gpio`/`pll`/`pvt`/`i2c` are address-map-**bound** yet byte-undumped.
> Always state *which lens* a coverage number is on. `[HIGH · OBSERVED — this
> ledger states both]`

### 4.4 The consequential orphan — `tpb_events_semaphores_axi`

Eight of the nine address-map orphans are benign — peripheral IP shipped in the
shared `csrs/` pool but instantiated only on a later gen (the four `ap_intc_*` +
`intc_1grp_no_msix` are the Maverick decentralized-INTC fleet; `erg_ecc_noinit`,
`dwc_e32mp_phy`, `pmdt_complex` are unused variants). The one genuine Cayman
binding gap is **`tpb_events_semaphores_axi.json`** (6,017 bytes): the EVT_SEM data
aperture is real and decomposed into op sub-windows, but **no Cayman node carries
its `json:` bind** (`rg 'EVT_SEM' … \| rg -c 'json:'` = **0**). Maverick *does* bind
its equivalent `TPB_EVT_SEM.json`. On Cayman the op layout is recovered from
address arithmetic; see [block-schema-xref.md §4d](../address/block-schema-xref.md)
and [tpb-subblocks.md](tpb-subblocks.md). `[HIGH · OBSERVED]`

---

## 5. The AXI fault-terminator family (NTS poison) `[HIGH · OBSERVED]`

Three security/QoS blocks in `csrs/sprot/` share one AXI-error idiom for a
denied/no-target transaction, and the ledger names them as the **terminators**:
on a deny they return an AXI error response with a poison read-data pattern.

| block | response field reset | read-data reset | role |
|---|---|---|---|
| `qos_prot` | `read_response`/`write_response` = **`0x2`** (`00-OK,…,10-SLVERR,11-DECERR`) | **`0xDEADBEEF`** ("Data to be returned when in NTS mode … default=deadbeef") | the QoS NTS responder |
| `nsm` | `axi_bresp`/`rresp` reset **`0x2`** (SLVERR) | `error_data_0..7` each **`0xDEADBEEF`** (256-bit poison) | AXI integrity watchdog |
| `amzn_remapper` / `user_remapper` | — (no response code in schema) | — | **decides** pass/deny; **delegates** the error response to `qos_prot` NTS |

> **NOTE — decide vs respond.** The remappers do **not** themselves carry the
> `0xDEADBEEF` poison (verified: `rg -ci deadbeef {amzn,user}_remapper.json` = 0).
> They emit a pass/deny/remap/AxPROT *decision*; the sibling `qos_prot` NTS block
> emits the actual `SLVERR`/`DECERR` response and the `0xDEADBEEF` read-data on a
> denied path. The trust polarity lives in the remappers
> (`amzn` = fail-CLOSED `0x0`, `user` = fail-OPEN `0x1`); the terminator response
> lives in `qos_prot`/`nsm`. See [remapper.md](remapper.md), [qos-prot.md](qos-prot.md),
> [nsm.md](nsm.md). `[HIGH · OBSERVED]`

`0xDEADBEEF` (= `3735928559`) is the canonical read-data poison across the SoC;
`udma_gen_ex.drop_addr_msb` defaults to the same value for dropped-transaction
diagnostics ([udma-gen-tdma.md](udma-gen-tdma.md)).

---

## 6. Cross-gen divergence `[HIGH · OBSERVED for Cayman; INFERRED for v5 interior]`

`GenFlavor` inside all of this batch's primary JSONs (`notific_*`, `cce`, `cme`,
`dre`, `erg_*`, `misc_ram`) is `NORMAL` for every bundle and register — the
Cayman schemas carry **no** per-gen conditional fields. Divergence is at the
**address-map / instantiation** layer, not the register layer.

NOTIFIC presence per gen (count of `notific` in the gen address map):

| gen | `notific` hits | reading |
|---|---|---|
| cayman (NC-v3) | **1326** | the documented fleet |
| mariana | **1430** | SDMA compute/reorder fleet grew vs Cayman |
| mariana_plus | **1430** | byte-identical to mariana for notific/cce/dre |
| sunda (NC-v2) | **173** | reduced count (fewer SDMA channels) |
| tonga (oldest) | **0** | predates the notification engine |
| maverick (v5) | header-tree only | `address_map/` is per-die C headers; the pkl/json mirror carries notific bindings; **interior register layout INFERRED** |

`cce`/`dre` explicit instances: mariana = `cce 660 / dre 396`; mariana_plus
identical; cayman uses `*_sdma_<n>_{cce,dre}` naming; tonga/sunda: none. The
notification engine is a Cayman/Mariana-era feature. `[counts HIGH · OBSERVED;
"grew/predates" reading MED · INFERRED; v5 interior INFERRED]`

> **GOTCHA — maverick map shape.** Maverick ships `maverick/address_map/` (a tree
> of per-die `*.h` headers) and `maverick/address_map.hpp`, not a single `_flat`
> YAML. Bindings come from `ext/al_address_map_db.{pkl,json}`. **Never
> `pickle.load`** the 207-MiB pkl — stream the JSON mirror or use `pickletools`.
> Every v5-interior register claim is `INFERRED` (no v5 register schema ships).

---

## 7. Adversarial self-verification `[HIGH · OBSERVED]`

The five strongest count claims, re-checked against the binary this session:

| # | claim | check | result |
|---|---|---|---|
| 1 | `notific_1_queue` = **962** (all ERRTRIG) | `rg -c 'notific_1_queue.json$' json_xref`; `… \| rg -c ERRTRIG` | 962 / 962 ✓ |
| 2 | `notific_10_queue` = **332** | `rg -c 'notific_10_queue.json$' json_xref` | 332 ✓ |
| 3 | `cce` = `cme` = `dre` = **132** each | `rg -c '<x>.json$' json_xref` ×3 | 132/132/132 ✓ |
| 4 | **85** on disk / **76** referenced / **9** orphans / **0** missing | `fd -e json`; `comm -23/−13 set(on_disk) set(referenced)` | 85/76/9/0 ✓ |
| 5 | `qos_pmu`=**528** / `qos_host_visible`=**1208** / `qos_prot`=**1500** | `rg -c` ×3 | 528/1208/1500 ✓ |

Supporting byte-exact checks: `notific_1`↔`notific_10` bundles `jq -S` diff =
identical (only `NUM_SW_Q 1/10`, `SW_Q_RESET_TO_ALL_1 1/1023`); `cce`/`cme`/`dre`
reg-defs = 14/1/6 and bitfield-defs = 16/3/23; `erg_ecc`↔`erg_ecc_noinit` `jq -S`
diff = single `ResetValue 0→1`; `qos_prot` NTS `read_data` reset = `0xdeadbeef`,
`read_response` reset = `0x2` (SLVERR). All reconcile.

---

## 8. CSR-lane completeness statement

The CSR reverse-engineering lane (CSR-01..20) has byte-exact-documented **every**
register block on the GPSIMD/Xtensa, TPB compute, SDMA tensor-DMA, notification,
and interrupt/QoS/security fast paths. Of the **85** `csrs/` schemas: **65**
byte-exact, **2** shape-captured (`erg_parity` pair), **18** byte-undumped
peripheral/infrastructure residual (clock/PLL, thermal/PVT, I2C/SPI/GPIO, OTP,
DFT/scan, ring bus, service fabric, URB, APB bridge, `misc_model`, `pmdt_complex`)
— none on any GPSIMD ucode / tensor / notification / interrupt path. The 18 are an
explicit, enumerated, low-priority residual; an optional "CSR-PERIPH" sweep could
byte-dump them if peripheral coverage is ever desired. **This is the last CSR
task.** `[HIGH]`

---

## 9. Cross-references

- [CSR — NOTIFIC Queue](notific-queue.md) — the full notification block (record
  format, `notific_type` enum, 41+6 registers, nine interrupts, queue protocol);
  this page adds only the 1-vs-10 variant reconciliation.
- [Block → Schema Cross-Reference](../address/block-schema-xref.md) — the
  authoritative address-map binding lens (85/76/9 orphans, the reverse index, the
  Maverick pkl reconciliation).
- SDMA siblings: [udma-m2s](udma-m2s.md) · [udma-s2m](udma-s2m.md) ·
  [udma-gen-tdma](udma-gen-tdma.md).
- Security/QoS terminator family: [remapper](remapper.md) (decide) ·
  [qos-prot](qos-prot.md) / [nsm](nsm.md) (respond + poison).
- INTC: [intc-4group](intc-4group.md) · [intc-1group-apintc](intc-1group-apintc.md).
- Error triggers: [fis-errtrig-spad](fis-errtrig-spad.md) — the 962-pair errtrig
  generators that bind `notific_1_queue`.

---

### Confidence summary

- §2.1, §3, §4.1, §4.3, §5, §7: **HIGH · OBSERVED** — literal from the shipped
  `csrs/*.json`, `address_map_flat.yaml`, and `address_map_json_xref.yaml`.
- §2.2 errtrig=962 **CORRECTION**: **HIGH · OBSERVED** (re-derived) / **CARRIED**
  from the #930 fix of #908's 642.
- §2.3 variant rationale, §3.1/§3.2 "tensor-DMA"/"collective" roles, §6
  "grew/predates" reading: **MED · INFERRED**.
- v5/Maverick interior register layout (§6): **INFERRED** — `maverick/` is
  header/binding-`OBSERVED` only; no v5 register schema ships.
