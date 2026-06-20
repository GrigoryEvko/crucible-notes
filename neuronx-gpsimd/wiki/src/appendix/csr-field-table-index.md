# The CSR Field-Table Index

This is the **consolidated, grep-friendly CSR reference** for the Cayman (NC-v3)
GPSIMD / Vision-Q7 SoC — the lookup companion to the twenty narrative CSR pages
under [`../control/csr/`](../control/csr/tpb.md). Where each narrative page reasons
about *one* register block in depth (semantics, boot contract, cross-gen drift), this
appendix is a **flat per-register / per-bitfield table** across every block: block →
register/bundle → offset → field → bit-range → access-type → reset → one-line meaning.
It is deliberately terse and one-bitfield-per-row where it matters, so a reimplementer
can `rg` for a register name and land on its offset, width, and reset in one hop.

Everything here is byte-grounded in the shipped, RTL-generated **`cayman-arch-regs`
CSR JSON schemas** (`csrs/<sub>/<unit>.json`) and their cross-gen `arch-headers`
copies — binary-derived register descriptors, citeable as static-analysis artifacts.
Every offset, width, and reset below was re-read from the JSON with `jq` by absolute
path. No vendor source tree was consulted; the schemas *are* the binary. Confidence
tags are `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED`; callouts are
`QUIRK / GOTCHA / NOTE / CORRECTION`.

> **How to read this page.** Each block section opens with a one-line identity
> (`UnitName`, `SizeInBytes`, `InterfaceType`, bundle/reg/field counts) and a link to
> its narrative page, then a row-per-bitfield index of its primary registers.
> **Absolute register offset = `bundle.AddressOffset + register.AddressOffset`** (+ the
> per-instance `ArraySize` stride for arrayed bundles — §1). The **SoC-absolute** address
> is that offset plus the block's leaf base from the [block→schema xref](../control/address/block-schema-xref.md)
> and the [unified SoC memory map](../control/address/unified-soc-memory-map.md). Literal
> `|` inside a cell is escaped `\|`.

**Related (read these for the *why*):**
the 20 narrative CSR pages (§3 index), the block→schema join
[`block-schema-xref.md`](../control/address/block-schema-xref.md), the unified region
table [`unified-soc-memory-map.md`](../control/address/unified-soc-memory-map.md), and
the per-gen applicability matrix [`master-capability-matrix.md`](../generations/master-capability-matrix.md).

---

## 1. Schema conventions — the JSON shape and the join `[HIGH · OBSERVED]`

Every CSR schema is one `RegFile` object. The nesting and the keys a reimplementer
parses are fixed across all 76 referenced schemas:

```
RegFile { UnitName, Type="REGFILE", RegfileFlavor, InterfaceType, AddrWidth,
          DataWidth, SizeInBytes, Parameters[], Memories[], RegistersBundleArrays[] }
  RegistersBundleArrays[] { Name, AddressOffset, ArraySize, BundleSizeInBytes, GenFlavor, Registers[] }
    Registers[] { Name, AddressOffset, AccessType, BitFields[] }
      BitFields[] { Name, Position("hi:lo" | "N"), AccessType, ResetValue, Description, SpecialAccess }
```

### 1a. AddressOffset is relative to the bundle base

A register's `AddressOffset` is **relative to its bundle's** `AddressOffset`. For an
**arrayed** bundle (`ArraySize > 1`) the per-instance base advances by `BundleSizeInBytes`:

```
abs_off(reg, i) = bundle.AddressOffset + i*BundleSizeInBytes + reg.AddressOffset   // i in 0..ArraySize-1
```

`BitFields[].Position` is a single bit (`"4"`) or an inclusive range (`"31:0"`) — **not**
`BitOffset`/`BitWidth`. The reset key is literally `ResetValue` (a hex *string*); a `.Reset`
lookup returns `null` for every field. Field access lives on the **`BitField`**, not the
register — the register-level `AccessType` is frequently `RW` while the bitfield is `RO`/`WO`
(e.g. `nsm.control.report`, `tpb_events_semaphores_axi` op windows). **Always read access
from the bitfield.**

### 1b. GOTCHA — the hex-vs-decimal `BundleSizeInBytes` (and `SizeInBytes`) radix trap

> **GOTCHA `[HIGH · OBSERVED]`.** `AddressOffset` is **always** a hex string. But
> `BundleSizeInBytes` and `SizeInBytes` are **per-file mixed radix** — hex (`"0x100"`) in
> most blocks, bare **decimal** (`"256"`, `"64"`) in several. **Concrete failure mode:**
> `udma_m2s.AXI_M2S_MLA.BundleSizeInBytes = "256"`. Read as hex (`strtol(...,16)` → `0x256`
> = 598) the very next bundle (`AXI_M2S @ 0x100`) lands *inside* the first bundle's span and
> every queue past `M2S_Q[0]` is mislaid. The truth is decimal `256` = `0x100`, which lands
> `AXI_M2S` exactly at the bundle boundary. The same trap closes the `intc_4grp_msix` aperture:
> `MSIX_Vector_Table_Space.BundleSizeInBytes = "16"` (decimal) gives `0x800 + 128×16 = 0x1000`
> == `SizeInBytes`; read as hex (`0x16`=22) it overflows to `0x1300`. **Radix census (re-derived):**

| radix of `BundleSizeInBytes` / `SizeInBytes` | blocks | example |
|---|---|---|
| **all hex** (`"0x100"`) | `tpb*`, `sprot/*` (`qos_*`, `nsm`, `*_remapper`), `intc` *hex bundles*, `xtensa_*`, `notific`, `erg`, `hbm/*`, `d2d/*`, `pcie/*`, `rdm_model` (sizes) | `nsm.control` BSIB `0x100` |
| **bare decimal** | `udma_gen` / `udma_gen_ex` (`BundleSizeInBytes` only), `udma_m2s`/`udma_s2m` (`BundleSizeInBytes` **and** `SizeInBytes`), `intc_*` *decimal bundles* (`PBA="4"`, `MSIX_VTS="16"`, `VecTable/Val="8"`), `gpio`/`pvt` (`SizeInBytes` `"4096"`/`"65536"`), `fis_control` / `rdm_model` (`AddressOffset` decimal too) | `udma_m2s.AXI_M2S_MLA` BSIB `256` = `0x100` |

> **Second-order trap — `AddressOffset` itself is decimal in two files.** `fis_control` and
> `rdm_model` use **decimal** register `AddressOffset`s (`fis_control` bundle bases
> `0,24,40,48,76,120,140`; `rdm_model` `queue_size_0 @ "32"` = `0x20`). Parse `AddressOffset`
> per-file, not globally as hex. `[HIGH · OBSERVED]`

### 1c. ArraySize is a per-instance stride — and may be a symbolic parameter

`ArraySize` replicates a bundle's register set `N` times at `BundleSizeInBytes` stride. It is
often a **literal** (`"40"`, `"16"`) but is sometimes a **parameter name** resolved from
`Parameters[]`: `notific_nq.ArraySize = "NUM_SW_Q"` → `Parameters[NUM_SW_Q].Value = "10"`;
`intc_4grp.ctrl.ArraySize = "INTC_NUM_GROUPS"` → `4`; `intc_msix.MSIX_Vector_Table_Space.ArraySize
= "NUM_OF_TRIGS"` → hardcoded `128`. A parser that treats `ArraySize` as always-numeric crashes
on the symbolic forms. The `ap_intc` wrappers go further — their `SizeInBytes` / `AddrWidth` are
**unevaluated expression strings** (`"0x40*INTC_NUM_GROUPS"`, `"log2(0x40*INTC_NUM_GROUPS)"`)
that a downstream generator resolves. `[HIGH · OBSERVED]`

### 1d. The block → schema → address join

A block name resolves to an absolute SoC address in three hops, joining the **CSR lane** (this
appendix, "what the registers are") to the **ADDR lane** ("where they live"):

```
(1) block leaf name      e.g.  TPB_0_POOL_LOCAL_REG
        │  block-schema-xref.md  (json: key in the flat YAML, basename = join key)
        ▼
(2) schema basename      e.g.  tpb/tpb_xt_local_reg.json   →  the field tables on THIS page
        │  unified-soc-memory-map.md / soc-master-map.md (leaf base from the flat YAML)
        ▼
(3) SoC-absolute address e.g.  0x2803060000 + 0x3000 + 0x0  =  0x2803063000  (q7 run-stall doorbell)
```

The forward bind is the flat YAML's inline `json:` key (present on **19,012** of 34,858 nodes;
absence = pure memory / reserved / container — *not* a missing-schema error). The same schema
basename binds many leaves (`tpb_xt_local_reg.json` binds **84** across TPB/TOP_SP/PEB_SP/PREPROC),
so one row here describes dozens of physical instances. SoC-absolute math, end to end, is worked in
[`unified-soc-memory-map.md §2`](../control/address/unified-soc-memory-map.md); the binding census
and the reverse index are in [`block-schema-xref.md`](../control/address/block-schema-xref.md).

> **GOTCHA — PEB-plane bit-53.** Privileged leaves live on the `PEB_APB_IO*` plane whose absolute
> bases carry **bit 53** (`0x20000000000000`); the die-1 copy ORs **bit 47** (`0x800000000000`).
> A block's *schema offsets* (this page) are identical on both planes — only the leaf base moves.
> `[HIGH · OBSERVED]`

---

## 2. The "20 CSR blocks" reconciliation `[HIGH · OBSERVED]`

> **CORRECTION / SCOPE — "20 CSR blocks" = the 20 narrative CSR pages, NOT 20 schemas.**
> `fd` over [`../control/csr/`](../control/csr/tpb.md) returns **exactly 20** `.md` pages
> (re-counted this session). They do **not** map 1:1 to 20 schema JSONs — they document the
> register surfaces *grouped by subsystem*, and several pages cover many schemas at once. The
> 20 pages collectively byte-document **65 of the 85** on-disk schemas (the other 20 are
> peripheral residual — clock/PLL, PVT, I2C/SPI/GPIO, OTP, DFT, ring, URB, APB bridge — see
> [`notific-sdma-residual.md §4`](../control/csr/notific-sdma-residual.md)). The "20 blocks"
> headline reproduces against the page count, not a schema count. Mapping each page to its
> schema(s):

| # | Narrative page (`../control/csr/…`) | Schema(s) it documents (`csrs/…`) |
|---|---|---|
| 1 | [tpb-xt-local-reg.md](../control/csr/tpb-xt-local-reg.md) | `tpb/tpb_xt_local_reg.json` |
| 2 | [tpb.md](../control/csr/tpb.md) | `tpb/tpb.json` |
| 3 | [pe-array-sequencer.md](../control/csr/pe-array-sequencer.md) | `tpb/tpb_arr_seq_top_host_visible.json` (+ `_protected`, `_cluster`) |
| 4 | [tpb-subblocks.md](../control/csr/tpb-subblocks.md) | `tpb/{tpb_sbuf_cluster,tpb_sbuf_pool_act,tpb_events_semaphores_axi,tpb_arr_seq_cluster_host_visible,tpb_arr_seq_top_protected,tpb_ham}.json` + `erg/erg_parity_model.json` |
| 5 | [udma-m2s.md](../control/csr/udma-m2s.md) | `sdma/udma_m2s.json` |
| 6 | [udma-s2m.md](../control/csr/udma-s2m.md) | `sdma/udma_s2m.json` |
| 7 | [udma-gen-tdma.md](../control/csr/udma-gen-tdma.md) | `sdma/{udma_gen,udma_gen_ex,tdma_model}.json` |
| 8 | [notific-queue.md](../control/csr/notific-queue.md) | `notific/notific_10_queue.json` |
| 9 | [notific-sdma-residual.md](../control/csr/notific-sdma-residual.md) | `notific/notific_1_queue.json`, `sdma/{cce,cme,dre}.json`, `erg/*`, `misc/misc_ram_model.json` + the 85-schema coverage ledger |
| 10 | [intc-4group.md](../control/csr/intc-4group.md) | `intc/intc_4grp_{no_msix,msix}_unit.json` |
| 11 | [intc-1group-apintc.md](../control/csr/intc-1group-apintc.md) | `intc/intc_1grp_{no_msix,msix}_unit.json`, `ap_intc/*` |
| 12 | [fis-errtrig-spad.md](../control/csr/fis-errtrig-spad.md) | `fis/{fis_control,papb_bcast}.json` (+ errtrig = `intc_4grp` PAIR + `notific_1_queue`) |
| 13 | [nsm.md](../control/csr/nsm.md) | `sprot/nsm.json` |
| 14 | [qos-prot.md](../control/csr/qos-prot.md) | `sprot/qos_prot.json` |
| 15 | [qos-pmu-hostvisible.md](../control/csr/qos-pmu-hostvisible.md) | `sprot/{qos_pmu,qos_host_visible}.json` |
| 16 | [remapper.md](../control/csr/remapper.md) | `sprot/{amzn_remapper,user_remapper}.json` |
| 17 | [rdm-top-sp.md](../control/csr/rdm-top-sp.md) | `rdm/rdm_model.json`, `top_sp/top_sp_ram.json` |
| 18 | [hbm-d2d-pcie-blocks.md](../control/csr/hbm-d2d-pcie-blocks.md) | `hbm/*`, `d2d/*`, `pcie/*`, `erg/erg_ecc_model.json` |
| 19 | [xtensa-q7.md](../control/csr/xtensa-q7.md) | `xtensa_q7/xtensa_q7.json` |
| 20 | [xtensa-nx.md](../control/csr/xtensa-nx.md) | `xtensa_nx/xtensa_nx.json` |

All 20 link paths were confirmed to resolve on disk this session. The schema *count* is the 85/76/65
ledger of [`notific-sdma-residual.md §4`](../control/csr/notific-sdma-residual.md); the *page* count
is 20.

---

## 3. The consolidated field-table index

Grouped by block. Each block names its schema, window, interface, and (bundles/regs/fields), then
indexes its primary registers. **Offsets are absolute within the regfile window** (bundle base +
register offset) unless a row is inside an arrayed bundle, where the per-instance stride is noted.

### 3.1 `tpb_xt_local_reg` — per-engine NX + 8×Q7 control `[HIGH · OBSERVED]`

Schema `tpb/tpb_xt_local_reg.json` · `0x10000` (64 KiB) · APB · 7 bundles / 55 regs / 84 fields ·
every reg+field `RW`, every `SpecialAccess=None`. Bound **84×** (TPB×40, TOP_SP×20, PEB_SP×20,
PREPROC×4) — the canonical shared Q7/SP IP. Page: [tpb-xt-local-reg.md](../control/csr/tpb-xt-local-reg.md).

Bundle map: `nx@0x0000`(arr 1), `general@0x1000`(arr 60, stride `0x20`), `window@0x2000`(arr 40,
stride `0x1C`), `q7@0x3000`(arr 1), `hw_decode@0x4000`(arr 1), `tensor_replace@0x5000`(arr 32,
stride `0x20`), `notific@0x6000`(arr 1).

| reg | abs off | field | bits | acc | reset | meaning |
|---|---|---|---|---|---|---|
| `nx/release_run_stall` | `0x0000` | `run_stall` | `0` | RW | `0x1` | write 0 → release NX RunStallOnReset; **reset 1 ⇒ held stalled** |
| `nx/start_ctrl` | `0x0004` | `ctrl` | `0` | RW | `0x0` | 1: start_addr valid, exit Halt |
| `q7/release_run_stall` | `0x3000` | `run_stall` | `7:0` | RW | `0xFF` | **per-core** stall bitmask; reset `0xFF` ⇒ all 8 Q7 stalled (GOTCHA: blind `=0` releases all 8) |
| `q7/start_ctrl` | `0x3004` | `ctrl` | `0` | RW | `0x0` | 1: start_addr valid, start executing |
| `q7/run_state_0..7` | `0x3008`–`0x3024` | `run_state` | `31:0` | RW | `0x0` | opaque per-core status word |
| `q7/intr_ctrl` | `0x3028` | `en` | `31:0` | RW | `0x0` | **4 bits per Q7** (core c → `[4c+3:4c]`) |
| `window[i]/control` | `0x2000`+`i*0x1C` | `window_valid` | `0` | RW | `0x0` | enable window — **set LAST**, after mask/match/replace |
| `window[i]/control` | (same) | `single_q7_select` | `6:4` | RW | `0x0` | which Q7 (0..7) when `single_q7_enable[3]`=1 |
| `window[i]/mask_lo`/`_hi` | `+0x04`/`+0x08` | `value` | `31:20`/`7:0` | RW | `0x0` | 40-bit mask key `[39:20]`, low 20 implicit-0 (1 MiB page) |
| `window[i]/replace_hi` | `+0x18` | `value` | `25:0` | RW | `0x0` | SoC output base `[63:32]` |
| `tensor_replace[d]/tensor_4d_dim_0` | `0x5000`+`d*0x20` | `step_size_x`/`_y` | `15:0`/`31:16` | RW | `0x0` | 4D descriptor: two `uint16` packed per word |
| `hw_decode/control` | `0x4000` | `disable_hw_decode` | `0` | RW | `0x0` | Instr-FIFO vs HW-decode select |
| `hw_decode/breakpoint_ctrl` | `0x4004` | `breakpoint_instr_enable` | `0` | RW | `0x1` | **armed at reset** (triggers reset 0) |
| `notific/sw_queue_num0` | `0x6000` | `Q7_0..Q7_3` | `3:0`..`15:12` | RW | `0x0` | SW-queue map — **only Q7_0..3** (no `sw_queue_num1`) |
| `notific/cfg_timestamp_inc` | `0x6004` | `val` | `23:0` | RW | `0xb2924` | timestamp increment (clock-derived) |

> **NOTE — per-gen.** v4/v5 (`mariana`/`mariana_plus`/`maverick`) append **4 atomic-op bundles**
> (`atomic_rdwr/inc/dec/addiff0 @0x8000–0x8FFF`, each arr 64) → 59 regs / 88 fields; all 7 Cayman
> bundles are byte-identical at the same offsets. Sunda/Tonga: schema **not shipped** (INFERRED
> present). `[HIGH · OBSERVED v3/v4/v5]`

### 3.2 `tpb` — top-level cluster datapath + event routing `[HIGH · OBSERVED]`

Schema `tpb/tpb.json` · `0x1000` (4 KiB) · APB · 9 bundles / 146 regs / 185 fields · bound 16×.
Page: [tpb.md](../control/csr/tpb.md). Bundles: `pe_sequencer@0x000`, `pool_sequencer@0x100`,
`act_sequencer@0x200`, `dve_sequencer@0x300`, `events_semaphores@0x800`, `notific@0xA00`,
`misc@0xB00`, `performance_counter@0xC00`, `intc_bypass@0xE00` (all arr 1).

| reg | abs off | field | bits | acc | reset | meaning |
|---|---|---|---|---|---|---|
| `pool/act/dve stochastic_rnd` | `0x10C`/`0x20C`/`0x30C` | `seed` | `20:0` | **WO** | `0x0` | 21-bit per-dtype stochastic-rounding PRNG seed (read-meaningless) |
| `…stochastic_rnd` | (same) | `dtype` | `31:28` | WO | `0x0` | target output dtype slot |
| `…stochastic_rnd_mode` | `+0x4` | `mode` | `0` | RW | `0x0` | 0=IEEE-RNE, 1=stochastic |
| `dve_sequencer/lfsr` | `0x314` | `seed` | `31:0` | WO | `0x0` | RNG-instruction LFSR seed |
| `pool/act/dve bias_adjust` | `0x108`/`0x208`/`0x308` | `bias` | `5:0` | RW | `0x0` | FP8 output-converter bias (absent on PE) |
| `act_sequencer/nan_val` | `0x248` | `val_NT_` | `31:0` | RW | `0x7fc00000` | FP32 qNaN compare value |
| `act_sequencer/pos_inf_val` | `0x250` | `val_NT_` | `31:0` | RW | `0x7f800000` | FP32 +Inf value |
| `events_semaphores/sem_threshold_ctrl0` | `0x800` | `low_NT_` | `31:0` | RW | `0x0` | semaphore low threshold |
| `events_semaphores/notific_ctrl` | `0x808` | `notifications_en` | `0` | RW | `0x0` | cluster event/sem notification master enable |
| `notific/sw_queue_num0` | `0xA00` | `pe_nx_NT_`…`pool_engine_NT_` | `3:0`..`27:24` | RW | `0x0` | per-source 4-bit SW-queue selectors |
| `notific/sw_queue_num5` | `0xA14` | `Q7_0`..`Q7_7` | `3:0`..`31:28` | RW | `0x0` | all **eight** Q7 cores (cf. xt_local_reg has only Q7_0..3) |
| `notific/queue_idx_ctrl` | `0xA18` | `<eng>_instr_queue_idx` | `0`..`4` | RW | `0x0` | 1 ⇒ queue index from instruction not register |
| `misc/fake_error_ctrl` | `0xB00` | `en` | `0` | RW **PulseOnW** | `0x0` | SW error-inject pulse |
| `misc/chicken_ctrl` | `0xB40` | `pool_dve_arb_en` | `0` | RW | `0x1` | POOL∥DVE parallel execute (**enabled at reset**) |
| `intc_bypass/intc_bypass_0..7` | `0xE00`–`0xE1C` | `bypass` | `31:0` | RW | `0x0` | 256 INTC-trigger bypass bits |

> **NOTE — gen-specific shape.** `tpb.json` is re-spun per gen: sunda 8 bundles/136 regs,
> **cayman 9/146/185**, mariana 12/168. v5 (`maverick`) restructures to `tpb_top.json` (+`tpb_dve`).
> `[HIGH · OBSERVED v2/v3/v4; v5 INFERRED]`

### 3.3 PE-array sequencer (host-visible / cluster / protected) `[HIGH · OBSERVED]`

Schemas `tpb/tpb_arr_seq_top_host_visible.json` (7 bundles / **305** regs / 308 fields; 299 RO / 6
RW; 1 PulseOnW), `tpb_arr_seq_cluster_host_visible.json` (3/17), `tpb_arr_seq_top_protected.json`
(1/1). All `0x1000` · APB · bound 16/16/8×. Page: [pe-array-sequencer.md](../control/csr/pe-array-sequencer.md).

| reg | abs off | field | bits | acc | reset | meaning |
|---|---|---|---|---|---|---|
| `host/arr_seq_cfg/queue_cfg` | `0x000` | `disable_dependency_check` | `8` | RW | `0x0` | single-thread the 16 queue slots |
| `host/perf_cntr_cfg` | `0x004` | `cntr_rst` | `4` | RW **PulseOnW** | `0x0` | reset all perf counters (the block's only side-effecting bit) |
| `host/…instr_cnt_lsb`/`_msb` | bank `0x100`/`0x300`/`0x500` | `cntr_lsb`/`cntr_msb` | `31:0`/`15:0` | RO | `0x0` | **48-bit** per-tile counter (49 tiles × 3 banks); MSB is `[15:0]` |
| `cluster/arr_cluster_cfg` | `0x000` | `matmul_done_last` | `8` | RW | `0x0` | done on last (vs first) SBUF read response |
| `cluster/arr_cluster_cfg` | (same) | `inter_instr_dly_cnt` | `21:16` | RW | `0x8` | inter-instruction response delay |
| `protected/throttle_cfg` | `0x000` | `disable_throttle` | `8:0` | RW | `0x1ff` | 9-lane XBUS throttle disable (reset all-off) |

> **CORRECTION (carried from #pe-array-sequencer §4).** Per-tile counters are **48-bit**
> (`lsb[31:0]` + `msb[15:0]`), NOT 64-bit; reconstruct `((u64)(msb&0xFFFF)<<32)|lsb`. The
> "go"/matmul launch is **not a CSR** — work enters via the micro-op IFIFO. `[HIGH · OBSERVED]`

### 3.4 TPB sub-blocks — SBUF / EVT_SEM / HAM `[HIGH · OBSERVED]`

Page: [tpb-subblocks.md](../control/csr/tpb-subblocks.md).

`tpb/tpb_sbuf_cluster.json` (`0x1000`, 5/10, bound 64×) — 2-stage SBUF port arbiter:

| reg | abs off | field | bits | acc | reset | meaning |
|---|---|---|---|---|---|---|
| `arb_stage2/arb_prio` | `0x100` | `pe_rd_client` | `18:16` | RW | `0x1` | matmul read port **wins by default** (only non-zero stage-2 prio) |
| `tdm_config/tdm_cfg` | `0x300` | `tdm_max_slots`/`tdm_dma_slots` | `15:0`/`31:16` | RW | `0x8`/`0x1` | TDM slot allocator |
| `axi2sram_config/axi2sram_transpose_en` | `0x400` | `transpose_en` | `0` | RW | `0x1` | AXI→SRAM transpose (default ON) |

`tpb/tpb_sbuf_pool_act.json` (`0x1000`, 4/16, bound 16×) — POOL/ACT/DVE token-bucket throttlers
(`rd/wr_throttle_cfg0.disable_throttle[0]` reset **`0x1`** = OFF; `cfg1.window_len[7:0]`/`transfer_cnt[23:16]`).

`tpb/tpb_events_semaphores_axi.json` (`0x100000` = 1 MiB, 5 bundles arr 256 stride `0x4`, IF=**APB**
despite `_axi` name) — 256 events + 256 semaphores, **op-aliased windows**:

| bundle | off | op | field acc |
|---|---|---|---|
| `tpb_events` | `0x0000` | event clear-0/set-1 | RW `[0]` |
| `tpb_semaphores_read` | `0x1000` | read current | RO `[31:0]` |
| `tpb_semaphores_set` | `0x1400` | overwrite | WO `[31:0]` |
| `tpb_semaphores_inc` | `0x1800` | atomic `+=` | WO `[31:0]` |
| `tpb_semaphores_dec` | `0x1C00` | atomic `-=` | WO `[31:0]` |

> **NOTE — EVT_SEM ResetValue is bare `"0"`** (no `0x`) for this file only; the threshold/notify
> *control* lives in `tpb.json events_semaphores@0x800` (§3.2). The container is bound only via
> address arithmetic on Cayman (schema is an **orphan** — §2). `[HIGH · OBSERVED]`

`tpb/tpb_ham.json` (`0x1000`, 7/30, bound 8×) — PE-array power/di-dt governor:

| reg | abs off | field | bits | acc | reset | meaning |
|---|---|---|---|---|---|---|
| `tpb_ham_arb/throttler` | `0x80C` | `{k,n,k_div_n}` | — | RO | **`0xFF`** ea | final ratio out (reset = 1:1, no throttle) |
| `tpb_ham_arb/winner` | `0x810` | `winner_vector` | `14:0` | RO | `0x4000` | one-hot; bit 14 = no_throttle slot |

> **QUIRK — `tpb_ham_table.ctrl` name↔description swap.** Bit `[28]` named `rd_en` but described
> "Write enable"; bit `[29]` named `wr_en` but described "Read enable". Schema bug — pin by the
> *description* (which matches the `entry_wr`/`entry_rd` dataflow) and verify against silicon.
> `[HIGH · OBSERVED]`

### 3.5 `udma_m2s` — outbound descriptor engine `[HIGH · OBSERVED]`

Schema `sdma/udma_m2s.json` · **`0x20000`** (`SizeInBytes="131072"` DECIMAL) · APB · 11 bundles /
100 regs / 325 fields · `M2S_Q` arr 16 stride `0x1000` · bound 280×. Page:
[udma-m2s.md](../control/csr/udma-m2s.md). **GOTCHA: `BundleSizeInBytes` is decimal here.**

| reg (Q-relative) | Q-off | field | bits | acc | reset | meaning |
|---|---|---|---|---|---|---|
| `M2S_Q/cfg` | `0x20` | `en_pref`/`en_scheduling` | `16`/`17` | RW | `0x0` | **queue enable** (start/stop a queue here) |
| `M2S_Q/TDRBP_low` | `0x28` | `addr` | `31:6` | RW | `0x0` | TX descriptor-ring base `[31:6]` (`[5:0]` RO-0, 64 B) |
| `M2S_Q/TDRL` | `0x30` | `descriptor_offset` | `23:0` | RW | `0x0` | ring length **in descriptors** |
| `M2S_Q/TDRTP_inc` | `0x38` | `val` | `23:0` | RW | `0x0` | **DOORBELL** — write N to advance tail by N |
| `M2S_Q/TDRHP` | `0x34` | `ring_id`/`descriptor_offset` | `31:30`/`23:0` | RO | `0x1` (ring_id) | head ptr (`ring_id` reset `0x1` = `1<<30` word) |
| `M2S_Q/TDRDTP_inc` | `0xe0` | `val` | `23:0` | **WO** | `0x0` | independent **data**-tail (NC-v3 enhanced prefetch) |
| `M2S_Q/comp_cfg` | `0xa0` | `en_comp_ring_update` | `0` | RW | `0x0` | completion writeback enable (**reset 0** vs S2M's 1) |
| `AXI_M2S/ostand_cfg` | `0x124` | — | — | RW | `0x20402040` | outstanding limits (comp_wr/comp_req/desc_rd/data_rd) |
| `M2S/cfg_len` | `0x2xx` | `max_pkt_size` | `19:0` | RW | `0x10000` | 64 KiB max packet (Maverick C header = 256 KiB) |
| `M2S_feature/dma_version` | `0x6xx` | `version` | — | RO | `0x04` | (Maverick header packs `0x10001880` — pin to build word) |

> **NOTE — per-gen.** Cayman == Mariana == Mariana+ (11/100/325). **Sunda** drops `AXI_M2S_MLA`,
> `enhanced_ostand_cfg`, `TDRDTP_inc`/`TDRDTP` → 10/94/316 (the 6-reg NC-v3 delta). v5 INFERRED
> (no schema). `[HIGH · OBSERVED]`

### 3.6 `udma_s2m` — inbound descriptor engine `[HIGH · OBSERVED]`

Schema `sdma/udma_s2m.json` · **`0x18000`** (`SizeInBytes="98304"` DECIMAL) · **`InterfaceType=NONE`**
(M2S is APB) · 8 bundles / 73 regs / 264 fields · `S2M_Q` arr 16 stride `0x1000` · bound 280×. Page:
[udma-s2m.md](../control/csr/udma-s2m.md). Mirror of M2S minus the 4 egress-shaping bundles, plus `S2M_wr`.

| reg (Q-relative) | Q-off | field | bits | acc | reset | meaning |
|---|---|---|---|---|---|---|
| `S2M_Q/cfg` | `0x20` | `en_pref`/`en_stream` | `16`/`17` | RW | `0x0` | queue enable (`en_stream` = RX analogue of M2S `en_scheduling`) |
| `S2M_Q/RDRTP_inc` | `0x38` | `val` | `23:0` | **WO** | `0x0` | **doorbell** — "posted N empty buffers" |
| `S2M_Q/comp_cfg` | `0x54` | `en_comp_ring_update` | `0` | RW | **`0x1`** | writeback **ON by default** (opposite of M2S) |
| `S2M_Q/pkt_cfg` | `0x5c` | `hdr_split_size` | `15:0` | RW | `0x40` | **S2M-only header split** |
| `S2M_wr/data_cfg_2` | `0x344` | (drop/hint/wait) | — | RW | `0x30002710` | RX no-descriptor policy: `hint_if_no_desc[28]`=1, `desc_wait_timer[23:0]`=`0x2710` |
| `AXI_S2M/data_wr_cfg_1` | `0x100` | (awid=3,awcache=3,awsize=5) | — | RW | — | DATA **write** AXI class (S2M writes, no data-read class) |

> **GOTCHA — `S2M_Q.BundleSizeInBytes="4096"` DECIMAL** (= `0x1000`). Hex-read (`0x4096`) puts
> `Q[15]@0x3d8ca` outside the `0x18000` window. `[HIGH · OBSERVED]`

### 3.7 `udma_gen` / `udma_gen_ex` / `tdma_model` — shared SDMA control `[HIGH · OBSERVED]`

Schemas `sdma/udma_gen.json` (`0x4000`, 22/69, **`BundleSizeInBytes` DECIMAL**, bound 280×),
`sdma/udma_gen_ex.json` (`0x4000`, 5/29, decimal BSIB, bound 280×), `sdma/tdma_model.json`
(`0x1000`, 8/44, **hex BSIB**, bound 264× — no broadcast mirror). Page:
[udma-gen-tdma.md](../control/csr/udma-gen-tdma.md).

| reg | abs off | field | bits | acc | reset | meaning |
|---|---|---|---|---|---|---|
| `gen/DMA_misc.revision` | `0x02104` | `major_id`/`minor_id`/`programming_id` | `31:24`/`23:12`/`11:0` | RO | `0x1c`/`0x360`/`0x03` | **only** version reg in the gen family |
| `gen/AXI.cfg_1` | `0x02300` | `tout` | `31:0` | RW | `0x02710` | AXI transaction timeout |
| `gen/AXI.cfg_2` | `0x02304` | `arb_promotion` | `3:0` | RW | `0x8` | **only** AXI-master arb knob (no per-q weights) |
| `gen/Mailbox[i].Interrupt` | `0x02200`+`i*64` | `set` | `0` | WO | `0x0` | cross-DMA doorbell |
| `gen_ex/VMPR[q].cfg_vmpr_0` | `0x000`+`q*64` | `tx_q_data_vmid_en` | `7` | RW | `0x0` | per-queue VMID enable (V4 virtualization) |
| `gen_ex/section_ctrl.drop_addr_msb` | `0x804` | `val` | `31:0` | RW | **`0xDEADBEEF`** | dropped-write poison target |
| `gen_ex/transaction_type_table.write_cmd` | `0xc04` | `val` | `1:0` | RW | — | route: 0=BUFF1 1=BUFF2 2=DDP 3=drop |
| `tdma/broadcast_cfg_group` | `0x100` | `group` | `15:0` | RW | `0xffff` | broadcast-group membership |
| `tdma/notific_cfg_per_queue[q].m2s_trigger` | `0x200`+`q*0x18` | `trigger_en`/`trigger_cnt` | `16`/`15:0` | RW | `0x0` | packet-count → notification |
| `tdma/app_engine_status.dre_packets_inflight` | `0x604` | — | `15:0` | RW | — | DRE (transpose) inflight per queue |

### 3.8 `notific_10_queue` / `notific_1_queue` `[HIGH · OBSERVED]`

Schemas `notific/notific_10_queue.json` (`0x1000`, 2/41+6, bound **332**×) and
`notific/notific_1_queue.json` (byte-identical bundles, `NUM_SW_Q=1`, bound **962**× — all
ERRTRIG sinks). `notific_nq` arr `NUM_SW_Q` stride `0x28`. Pages:
[notific-queue.md](../control/csr/notific-queue.md), [notific-sdma-residual.md](../control/csr/notific-sdma-residual.md).

| reg | abs off | field | bits | acc | reset | special | meaning |
|---|---|---|---|---|---|---|---|
| `notific/timestamp_lo`/`_hi` | `0x00`/`0x04` | `timestamp` | `31:0` | RO | `0x0` | None | 64-bit free-running 1 ps counter |
| `notific/nq_enable` | `0x14` | `en` | `NUM_SW_Q-1:0` | RW | `0x0` | None | per-SW-NQ enable; write to disabled NQ → drop + `intr_1` |
| `notific/nq_axi_id` | `0x2c` | `id` | `15:0` | RW | **`0x1`** | None | AXI ID for all writes |
| `notific/coal_timer` | `0xb4` | `coal_timer` | `31:0` | RW | `0x1000` | None | coalescer flush timer |
| `notific/timestamp_inc` | `0xcc` | `val` | `23:0` | RW | `0x0400` | None | per-tick timestamp increment |
| `notific_nq[i]/base_addr_lo` | `0x100`+`i*0x28` | `bits` | `31:0` | RW | `0x0` | **PulseOnW** | NQ ring base `[31:0]` |
| `notific_nq[i]/head_ptr` | `+0x0c` | `bits` | `31:0` | RW | `0x0` | **PulseOnW** | consumer ptr; write = drain-ACK + re-arm `intr_6` |
| `notific_nq[i]/tail_ptr` | `+0x10` | `tp` | `31:0` | RO | `0x0` | None | producer ptr (does NOT guarantee AXI drain) |

> **NOTE.** Nine interrupts `tpb_notific_intr_0..8`; the 16-byte wire record + `notific_type` enum
> live on the narrative page. Variant diff = only `NUM_SW_Q` (1 vs 10) + `SW_Q_RESET_TO_ALL_1`
> (1 vs 1023). `[HIGH · OBSERVED]`

### 3.9 `intc_4grp` / `intc_1grp` / `ap_intc` `[HIGH · OBSERVED]`

Schemas `intc/intc_4grp_{no_msix,msix}_unit.json` (`0x1000`, APB, bound 1070/858×),
`intc/intc_1grp_{no_msix,msix}_unit.json` (= 4grp at `INTC_NUM_GROUPS=1`; only `1grp_msix` placed,
4×), `ap_intc/*` (MEM IOFIC, all orphans). `ctrl` arr `INTC_NUM_GROUPS` stride `0x40`. Pages:
[intc-4group.md](../control/csr/intc-4group.md), [intc-1group-apintc.md](../control/csr/intc-1group-apintc.md).
Per-group register bank (abs = `g*0x40 + rel`):

| reg | rel | field | bits | acc | reset | meaning |
|---|---|---|---|---|---|---|
| `ctrl/int_cause_grp` | `0x00` | `val` | `31:0` | RW | `0x0` | latch — HW set, SW clears via **W0C** (Annapurna; NOT W1C) |
| `ctrl/int_cause_set_grp` | `0x08` | `int_cs_set` | `31:0` | WO | `0x0` | W1S software inject |
| `ctrl/int_mask_grp` | `0x10` | `int_msk` | `31:0` | RW | `0xffffffff` | per-bit mask — **all masked at reset** |
| `ctrl/int_mask_clear_grp` | `0x18` | `int_msk_clr` | `31:0` | WO | `0x0` | W0C side-door to unmask |
| `ctrl/int_cdc_bypass_grp` | `0x24` | `int_cdc_bypass` | `31:0` | RW (no_msix) / **RO "Unused"** (msix) | `0x0` | CDC edge-gen bypass |
| `ctrl/int_control_grp` | `0x28` | `rev_id` | `29:28` | RO | `0x1` | INTC revision (=1) |
| `ctrl/int_abort_msk_grp` | `0x30` | `int_abort_msk` | `31:0` | RW (no_msix) / **RO** (msix) | `0xffffffff` | abort severity wire-OR mask |
| `ctrl/int_error/fatal/log_msk_grp` | `0x2C`/`0x34`/`0x38` | `int_*_msk` | `31:0` | RW | `0xffffffff` | the other 3 severity masks |
| `ctrl/int_posedge_grp` | `0x3C` | `int_posedge` | `31:0` | RW | `0x0` | per-bit edge(1)/level(0) |

msix-only tail: `Sunda.MSIX_TC@0x3E0[2:0]`; `PBA@0x3F0` (arr 4, BSIB `4` **dec**); `VecTable@0x400`
(arr 4, BSIB `0x100`, nests `Val` arr 32 BSIB `8` **dec**); `MSIX_Vector_Table_Space@0x800` (arr
`NUM_OF_TRIGS`=128, BSIB `16` **dec**). `ap_intc_grp_ctrl` (9 regs, MEM/NONE) differs: drops
`int_cdc_bypass`/`int_error_msk`/`int_posedge`, and **`int_mask_grp` resets to `0` (UNMASKED)** —
the reverse of APB intc.

> **QUIRK — `ap_intc` boots UNMASKED** (`int_mask_grp` reset `0` vs APB `0xffffffff`). Firmware
> must mask before enabling sources. **GOTCHA — `1grp_msix` over-provisions** the MSI-X table at
> 128 entries even though only 32 cause bits are live. `[HIGH · OBSERVED]`

### 3.10 `fis_control` / `papb_bcast` — FIS control + errtrig `[HIGH · OBSERVED]`

Schemas `fis/fis_control.json` (`0x2000`, **decimal offsets**, AMZN-only, bound 582×),
`fis/papb_bcast.json` (`0x800`, USER, bound 464×). errtrig = `intc_4grp` PAIR + `notific_1_queue`.
Page: [fis-errtrig-spad.md](../control/csr/fis-errtrig-spad.md).

| reg | abs off (dec) | field | bits | acc | reset | meaning |
|---|---|---|---|---|---|---|
| `fis_control desc/fis_id` | `4` | `block_id` | `8:0` | RO | `0x0` | this FIS's block ID (RO topology descriptor) |
| `fis_control sw_cntrl/clk_enable` | `76` | `en` | `9:0` | RW | `0x3ff` | 10 sub-block clocks — all ON at reset |
| `fis_control sw_cntrl/apb_user_decode` | `100` | `user_fis_en`/`user_debug_en` | `16`/`24` | RW | `1`/`1` | region gates (qos_host_visible / qos_pmu) — both ON |
| `fis_control sw_cntrl/perf_snapshot_lo_0` | `28` | `count` | `31:0` | RW | `0x0` | **48-bit** window-0 snapshot (drives qos_* freeze) |
| `fis_control apb_timeout/ctrl` | `140` | `limit` | `31:0` | RW | **`0x2000`** | AMZN-chain APB watchdog (8192 cyc; 0=off) |
| `papb_bcast grps/mask` | `0` | `val` | `15:0` | RW | **`0xfffe`** | 16-group bcast membership; grp0 unicast (bit 0 dont-care) |

> **GOTCHA — `fis_control` uses DECIMAL `AddressOffset`s** (bundle bases `0,24,40,48,76,120,140`),
> the lone exception being the hex `SizeInBytes="0x2000"`. Read `perf_snapshot_lo_0 @28` as decimal
> 28, not `0x28`. `[HIGH · OBSERVED]`

### 3.11 `nsm` — AXI network-security monitor `[HIGH · OBSERVED]`

Schema `sprot/nsm.json` · `0x1000` · APB · 4 bundles / 50 regs / 74 fields · bound 220×.
Page: [nsm.md](../control/csr/nsm.md). Bundles `control@0x000`, `wr@0x100`, `rd@0x200`,
`spare@0x900`. Register offsets are **mixed-radix** (decimal `control.bypass@4`, hex thereafter).

| reg | abs off | field | bits | acc | reset | special | meaning |
|---|---|---|---|---|---|---|---|
| `control/bypass` | `0x004` | `enable` | `0` | RW | **`0x1`** | None | monitor **bypassed at reset** (FW arms by clearing) |
| `control/reset_staging_fifo` | `0x008` | `aw` | `0` | RW | `0x0` | **PulseOnW** | clear AW staging FIFO (only `aw` is PulseOnW) |
| `wr/cfg_1` | `0x118` | `axi_bresp` | `1:0` | RW | **`0x2`** | None | injected BRESP — `0x2`=SLVERR default |
| `rd/cfg_1` | `0x218` | `axi_rresp` | `1:0` | RW | **`0x2`** | None | injected RRESP = SLVERR (full word reset `0x3a`: + on_long/short/spurious) |
| `rd/error_data_0..7` | `0x21c`–`0x238` | `val` | `31:0` | RW | **`0xdeadbeef`** | None | 256-bit poison read payload (8×`0xDEADBEEF`) |
| `wr/status` | `0x100` | `error_3_awvalid_to_awready_timeout` | `12` | RO | `0x0` | None | AW handshake-stall cause (4 wr causes) |
| `rd/status` | `0x200` | `error_0_rlast_before_last_rdata` | `0` | RO | `0x0` | None | short read (5 rd causes — RLAST integrity is read-only) |
| `rd/sta_pillm_0` | `0x254` | `empty` | `31:0` | RO | **`0xffffffff`** | None | per-ID empty status (Cayman/Mav/Mariana; Sunda resets `0x0`) |

> **QUIRK — verbatim typo `isolatio_sm_nsm_axi_timeout_detected`** (missing `n`) in the pcie trigger
> YAML, index `[11]`. A symbol-table-faithful rebuild reproduces it. `[HIGH · OBSERVED]`

### 3.12 `qos_prot` — FIS QoS shaper + NTS `[HIGH · OBSERVED]`

Schema `sprot/qos_prot.json` · `0x1000` · APB · 5 bundles / 74 regs / 192 fields · 15 PulseOnW ·
all-hex sizes · bound 1500× (secure-only). Page: [qos-prot.md](../control/csr/qos-prot.md).
Bundles `csr@0x000`, `nts_amzn@0x400`, `wr_serializer@0x500`, `nts_isolation@0x600`, `spare_amzn@0x7F0`.

| reg | abs off | field | bits | acc | reset | meaning |
|---|---|---|---|---|---|---|
| `csr/control` | `0x000` | `chicken` | `0` | RW | **`0x1`** | disable ALL shaping at reset (boots transparent) |
| `csr/outstanding_transaction_limits` | `0x010` | `read_limit`/`write_limit` | `6:0`/`12:8` | RW | `0x0` | `0`→max (128 rd / 32 wr) |
| `csr/fairness_control` | `0x020` | `block_id_mask`/`interval` | `17:9`/`23:18` | RW | `0x7f`/`0xa` | masked-Block-ID windowed quota (the prio-cap, NOT DWRR) |
| `csr/utilization_control` | `0x024` | `clocks_in_interval` | `26:0` | RW | `0x400` | rate-window length |
| `csr/lfsr_ar_prob_seed` | `0x200` | `val` | `31:0` | RW **PulseOnW** | `0xFFFFFFFF` | 15 LFSRs (5 ch × 3), write reloads |
| `nts_amzn/read_response` | `0x408` | `val` | `1:0` | RW | **`0x2`** | NTS read resp = SLVERR |
| `nts_amzn/read_data` | `0x40c` | `val` | `31:0` | RW | **`0xDEADBEEF`** | NTS poison read-data (replicated across bus) |
| `nts_isolation/ctrl` | `0x600` | `rd_timeout_en` | `2` | RW | **`0x1`** | read timeout armed at reset |
| `wr_serializer/clear` | `0x508` | `clear_error` | `0` | **WO** | `0x0` | the file's only WO register |

### 3.13 `qos_pmu` / `qos_host_visible` — FIS QoS observe surfaces `[HIGH · OBSERVED]`

Schemas `sprot/qos_pmu.json` (`0x800`, 1 bundle `csr`, 146/154, RW-dominant profiler, bound 528×)
and `sprot/qos_host_visible.json` (`0x800`, 3 bundles, 64/67, RO-dominant monitor, bound 1208×).
All-hex sizes. Page: [qos-pmu-hostvisible.md](../control/csr/qos-pmu-hostvisible.md).

| reg | abs off | field | bits | acc | reset | meaning |
|---|---|---|---|---|---|---|
| `pmu csr/qos_pmu_intr_sts` | `0x000` | `val` | `15:0` | RO | `0x0` | 16-bit counter intr status → `fis_sprot_intr[4]` |
| `pmu csr/pmu_counterI_event_select` | `0x100`+`I*0x20` | `val` | `31:0` | RW | `0x0` | **one-hot** event select (boots disabled) |
| `pmu csr/pmu_counterI_threshold_hi` | `+0x08` | `val` | `31:0` | RW | `0x0` | 64-bit threshold high half |
| `pmu csr/pmu_counterI_snap0_hi` | `+0x14` | `val` | `15:0` | RO | `0x0` | **48-bit** snapshot counter |
| `pmu csr/axi_txn_matcherM_reg0` | `0x200`+`M*0x50` | `val` | `31:0` | RW | `0x0` | 640-bit CAM pattern (4 matchers × 20×32b) |
| `host nts_user/outstanding_reads` | `0x420` | `count` | `7:0` | RO | `0x0` | live NTS occupancy (8-bit = cap-width+1) |
| `host qos_user/read_channel_delta` | `0x0d0` | `ar_delta` | `8:0` | RO | `0x0` | signed 9-bit delta → `fis_sprot_intr[1]` underflow |
| `host qos_user/total_bytes_read_hi` | `0x00c` | — | `15:0` | RO | `0x0` | **48-bit** byte counter (observe-wider-than-36-bit-cap) |

### 3.14 `amzn_remapper` / `user_remapper` — FIS address/ID firewall `[HIGH · OBSERVED]`

Schemas `sprot/amzn_remapper.json` (`0x1000`, 10/87/134, privileged, bound 712×) and
`sprot/user_remapper.json` (`0x800`, 4/28/45, guest, bound 528×). Page:
[remapper.md](../control/csr/remapper.md). CAM = 6-word/192-bit indirect entry.

| reg | abs off | field | bits | acc | reset | meaning |
|---|---|---|---|---|---|---|
| `amzn control/amzn_cam_pass_on_miss` | `0x000` | `rd_pass_on_miss`/`wr_pass_on_miss` | `4`/`0` | RW | **`0x0`** | **fail-CLOSED** — miss = DENY (the isolation primitive) |
| `user control/user_cam_pass_on_miss` | `0x000` | `rd/wr_pass_on_miss` | `4`/`0` | RW | **`0x1`** | **fail-OPEN** — miss = PASS |
| `amzn control/master_prot` | `0x010` | `arprot`/`awprot` | `6:4`/`2:0` | RW | `0x2`/`0x2` | AxPROT generator (AMZN-only; non-secure-priv-data) |
| `*_cam/rd_buf_1` | `0x104` | `valid`/`wr_pass`/`rd_pass`/`remap_en` | `31`/`30`/`29`/`26` | RO | — | per-entry CAM verdict bits |
| `*_cam/rd_buf_1` | (same) | `id_cmp_dis` | `28` | RO | `0x0` (amzn) / `0x1` (user) | the **one** structural CAM reset diff |
| `*_cam/rd_buf_5` | `0x114` | `id` | `25:16` | RO | — | 10-bit AXI master-ID (1024 masters) |
| `amzn control/user_cam_ctl` | `0x020` | `cam_clr` | `0` | WO | `0x0` | privileged wipe of the guest CAM |
| `amzn control/addr_denied_lo` | `0x060` | `addr` | `31:0` | RO | `0x0` | denied-txn `[57:0]` capture (ISR latch) |

> **WALL — never invert `pass_on_miss`.** `amzn`→fail-open removes the firmware whitelist;
> `user`→fail-closed deadlocks every un-provisioned guest. Frozen Sunda=Cayman=Mariana. The
> remapper **DECIDES**; `qos_prot` NTS **RESPONDS** (SLVERR + `0xDEADBEEF`). `[HIGH · OBSERVED]`

### 3.15 `rdm_model` / `top_sp_ram` — RDM + TOP_SP config `[HIGH · OBSERVED]`

Schemas `rdm/rdm_model.json` (`0x1000`, 1 bundle, 236/242, **decimal offsets**, bound 4×) and
`top_sp/top_sp_ram.json` (`0x1000`, 1 bundle, 8/11, **hex offsets**, bound 40×). Page:
[rdm-top-sp.md](../control/csr/rdm-top-sp.md). Per-queue RDM block = 9 regs × 24 queues, **36-byte
stride** (decimal `i*36`).

| reg | abs off | field | bits | acc | reset | meaning |
|---|---|---|---|---|---|---|
| `rdm queue_base_hi_m2s_0` | `4` (dec) | `data` | `25:0` | RW | `0` (q0; q1–11 reset = `i`) | M2S ring base `[57:32]` (57-bit ring base) |
| `rdm queue_size_0` | `32` (dec) | `data` | `21:0` | RW | **`10`** | size in 4×16B-descriptor units |
| `rdm control` | `904` (dec) | `pop_one`/`pop_all` | `3`/`4` | RW | `0x0` | linked-list drain after error |
| `rdm ctrl` | `920` (dec) | `clkgate_req` | `0` | RW | **`1`** | gate the unused read auto-responder (RDM writes only) |
| `rdm ringid_initial` | `928` (dec) | `value` | `31:0` | RW | **`0x55555555`** | 2 bits/queue initial ringId |
| `top_sp_ram cfg/timestamp_inc` | `0x4` | `value` | `23:0` | RW | **`0x400`** | tsync tick (TPB-SP + semaphore blocks) |
| `top_sp_ram cfg/top_sp_whoami` | `0x8` | `value` | `8:0` | RO | **`0xff`** | BlockID (top_sp0-9 ⇒ 0x98–0xa1); **sunda `[7:0]`** |
| `top_sp_ram cfg/sw_queue` | `0x10` | `sp_nx_nt`…`errors_nt` | `3:0`..`15:12` | RW | `0x0` | 4 notification-class SW-queue selectors |

### 3.16 HBM / D2D / PCIe blocks `[HIGH · OBSERVED]`

Page: [hbm-d2d-pcie-blocks.md](../control/csr/hbm-d2d-pcie-blocks.md). `GenFlavor=EXTERNAL_IP`
on bought vendor IP, `null` on Amazon glue/RAS. All-hex sizes (decimal `SizeInBytes` recurs only
in Mariana's `hbm_xbar_port_hbm`). Representative control registers (full 41k-field PHY dumps
are out of scope — these are the control/init/RAS/link-state anchors):

| block · schema | reg | abs off | field | bits | acc | meaning |
|---|---|---|---|---|---|---|
| HBM ctrl · `hbm/ddr_csr_apb` (EXTERNAL_IP, `0x10000`) | `MC_BASE3.STAT_INTERRUPT_0` | `0x4934` | — | `31:0` | RO (W1C) | 32-bit interrupt vector |
| | `ECC_CONFIG.CFG_ECC_1BIT_INT_THRESH` | `0x6424` | `cfg_…` | `7:0` | RW | SBE interrupt threshold |
| | `DFI.STAT_DFI_CATTRIP` | `0x504c` | `stat_dfi_cattrip` | `0` | RO | catastrophic temp trip (critical fast-path) |
| HBM scrubber · `hbm/hbm_scbr` (null) | `sbr.cfg_ctl_ch_0` | `0x0` | `enable_scrbr` | `0` | RW | BIST/March scrubber enable |
| HBM page-retire · `hbm/hbm_hpr` (null) | `swap_done_ch_0` | `0x10` | `done` | `0` | RO | page-swap complete → trigger |
| D2D ctrl · `d2d/snps_ctrl` (EXTERNAL_IP, DWC-PCIe-as-D2D) | `PF0_AER_CAP.UNCORR_ERR_STATUS_OFF` | `0x104` | (bit→trigger 1:1) | `31:0` | RW | AER uncorrectable status |
| | `PF0_RAS_DES_CAP.SD_STATUS_L1LTSSM_REG` | `0x2b0` | `LTSSM_VARIABLE` | `31:16` | RO | LTSSM link-state |
| D2D ERG · `erg/erg_ecc_model` (null, `0x40`) | `uncerr_cnt` | `0x18` | — | `7:0` | RO | uncorrectable count (saturates `0xFF`) |
| Host PCIe · `pcie/pcie5_x8_dwc_pcie_ctl` (null, `0x2000`) | `SD_STATUS_L1LTSSM_REG` | `0x2b0` | — | — | RO | host-bridge LTSSM (adds MSI/MSIX/SPCIE caps) |

> **GENERATION WALL.** This block is Cayman/NC-v3 (DWC-PCIe + Marvell-XSR D2D, 16-channel HBM
> via `hbm_cfg.hbm_ctrl_debug` arr 16). The Maverick siblings'`32 HBM_CTRL_DP` / `8×32 XBAR` /
> `39 UCIE links` / `128 GiB` are **v5** — do not read them as Cayman. `[HIGH · OBSERVED]`

### 3.17 `xtensa_q7` / `xtensa_nx` — Cadence debug/trace/PMU/OCD `[HIGH · OBSERVED]`

Schemas `xtensa_q7/xtensa_q7.json` (`0x4000`, 5 bundles, **78/296**, bound 80×) and
`xtensa_nx/xtensa_nx.json` (`0x4000`, 5 bundles, **73/275**, bound 60×). Both APB, AddrWidth 14.
NX = Q7 **minus 5 registers** (`DIR4..7`, `FAULTINFOHI`) **plus 1 reset delta** (`TRAXID.CFGID`).
Pages: [xtensa-q7.md](../control/csr/xtensa-q7.md), [xtensa-nx.md](../control/csr/xtensa-nx.md).
Bundles `Trax@0x0000`, `Performance_Monitor@0x1000`, `OCD@0x2000`, `Miscellaneous@0x3000`,
`CoreSight@0x3F00`.

| reg | abs off | field | bits | acc | reset (Q7 / NX) | meaning |
|---|---|---|---|---|---|---|
| `Trax/TRAXID` | `0x0000` | `CFGID` | `15:0` | RO | `0x6A00` / **`0x6200`** | config hash — the **only** shared-reg reset delta (discriminates the cores) |
| `Trax/TRAXSTAT` | `0x0008` | `MEMSZ` | `12:8` | RW | `13` (both) | TraceRAM = 2¹³ = 8 KiB (same on both) |
| `PMU/PMG` | `0x1000` | `PMEN` | `0` | RW | `0x0` | global PMU enable (8 counters, identical NX=Q7) |
| `PMU/PMCTRLn` | `0x1100`+`n*4` | `SELECT`/`MASK` | `12:8`/`31:16` | RW | `0x0` | event-class selector (catalogue NOT in JSON) |
| `OCD/DSR` | `0x2010` | `Stopped` | `4` | RO | `0x0` | core under OCD control (25-field status word) |
| `OCD/DIR0EXEC` | `0x201c` | — | `31:0` | RW | — | write loads+executes injected instr |
| `OCD/DIR0..DIR7` | `0x2020`–`0x203c` | `DIRn` | `31:0` | RW | — | **Q7 has 8 words (256-bit); NX only DIR0..3 (128-bit)** |
| `Misc/FAULTINFOHI` | `0x3030` | (17 ECC flags) | — | RO | — | **Q7-only** per-RAM ECC breakdown (absent on NX) |

> **NOTE — 8 placeholder `0xb1` resets** on both cores (`ITCTRL`/`CLAIMSET`/…/`DEVTYPE`) reuse the
> `Component_ID3=0xB1` byte = generator default, **LOW** confidence on those resets (offsets/names
> HIGH). The 9th `0xb1` is the legitimate `Component_ID3`. `[HIGH offsets · LOW those resets]`

---

## 4. Per-gen CSR applicability — which rows are Cayman-only `[HIGH · OBSERVED / where flagged INFERRED]`

The block→schema xref's three-way partition (44 SHARED basenames + 32 Cayman-only) plus the
per-block cross-gen sections of the narrative pages give the applicability for every block above.
**Flag where the JSON is header-OBSERVED only (Maverick interior) vs byte-grounded.**

| block | Cayman (NC-v3) | cross-gen status | Maverick (v5) |
|---|---|---|---|
| `tpb_xt_local_reg` | 7 bundles / 55 / 84 | **frozen core**; v4/v5 **add** 4 atomic bundles (additive) | byte-id core + `atomic_*` (header-OBSERVED) |
| `tpb` | 9 / 146 / 185 | **re-spun per gen** (sunda 8, mariana 12) | restructured → `tpb_top.json` + `tpb_dve` (INFERRED interior) |
| `tpb_events_semaphores_axi` | 5/5/`0x100000`, **orphan bind** | INVARIANT v2–v4 | **ABSENT** (relocated under `tpb_top` reorg) |
| `tpb_arr_seq_top_protected` | 1/1 (`throttle_cfg`) | sunda 1/1 | mariana+ 1/12 (adds power rampdown) |
| `udma_m2s` | 11/100/325 (+`MLA`,`TDRDTP`) | **Cayman==Mariana==Mariana+**; **Sunda 10/94** (no NC-v3 prefetch) | superset (no schema — header `al_udma_m2s_regs.h`) |
| `udma_s2m` | 8/73/264, `IF=NONE` | **Sunda byte-identical** | superset (adds LMA, header-OBSERVED) |
| `udma_gen` | 22/69 | Cayman==Mariana; **Sunda older revision** (`0x02/0x01/0x01`) | — |
| `udma_gen_ex` | 5/29 | **identical all 4 gens** (V4 virt stable) | — |
| `tdma_model` | 8/44 | **fastest-evolving** (sunda 6, mariana 19) | — |
| `notific_*` | INVARIANT format | SCALED count; absent on Tonga | header-tree only (interior INFERRED) |
| `intc_4grp_*` | INVARIANT | byte-identical v2–v5 | keeps only `4grp` (drops `intc_1grp`) |
| `ap_intc_grp_ctrl` | 9 reg, MEM/NONE | == v2–v4 | **SEC-FORK** (12 reg, APB, `int_sec_grp`) |
| `fis_control` | 7 bundles | mariana(+) **+`fis_cntrl_intr`** bundle; **sunda no `fis_control`** | RESTRUCTURED → `tpb_top` (cf. tpb) |
| `nsm` | 4/50/74 | **cayman==mariana==mariana+==maverick** byte-id; sunda 1 reset diff | byte-identical (`nsm.json` shipped on v5) |
| `qos_prot` | 5/74/192 | **sunda 4/73** (no `nts_isolation`); mariana+ **10/105** (+AXI parity) | mariana shape (interior INFERRED) |
| `qos_pmu`/`qos_host_visible` | 146/64 | Cayman authoritative; thin v-gen copies | header-OBSERVED; `intr[5]` repurposed |
| `*_remapper` | amzn 10/87, user 4/28 | **frozen `pass_on_miss`/AxPROT**; Cayman widened ID 8→10 bit | enforcement resets byte-id Cayman↔Maverick |
| `rdm_model` | 236/24q/242 | **byte-identical** v2–v4 | header-only (`rdm.h`) |
| `top_sp_ram` | 8/11 | cayman==mariana; **sunda `whoami` differs** | NODE header only (interior INFERRED) |
| `hbm/ddr_csr_apb` | PS0/PS1 split, `0x10000` | **Sunda flat `0x40000`** (no PS); scrubber/HPR Cayman+ | v5 dropped PS split, 19 bundles (#906) |
| `d2d` stack | DWC-PCIe + Marvell-XSR (8 units) | **Sunda no D2D**; mariana single `d2d_wrap` | **native UCIE** (re-IP) |
| `xtensa_q7`/`xtensa_nx` | 78/296 · 73/275 | **carried verbatim** v3/v4/v5 | same JSON in maverick tree (silicon v5 INFERRED) |

> **NOTE — SUNDA-retired / v5-only rows, INFERRED where header-OBSERVED.** SUNDA-retired:
> `nts_isolation` (qos_prot), `txn_len_chk` (amzn_remapper), `dve_throttler` (sbuf_pool_act),
> `tpb_ham_notifi`, the NC-v3 prefetch regs (`AXI_M2S_MLA`/`TDRDTP`). v5/Maverick-only (the JSON is
> *header*-OBSERVED, interior INFERRED): `ap_intc_8grp_msix_unit`, `int_sec_grp`/`int_regs_sec_grp`
> SWOM, `SEMAPHORE_CNTR_INC` EVT_SEM port, `tpb_fab_remap*`, the H-die `hdie_spad`. Every Maverick
> *interior* behavioral claim is `[INFERRED]`; the Cayman field tables are `[HIGH · OBSERVED]`.

---

## 5. Verification ledger (this session)

The five strongest claims re-challenged directly against the JSON before authoring — all pass:

| # | claim | check (jq, absolute path) | result |
|---|---|---|---|
| 1 | **20** CSR blocks = 20 narrative pages | `fd -e md ../control/csr` | **20** ✓ (NOT 20 schemas — §2 CORRECTION; 85 on disk / 76 bound / 65 byte-documented) |
| 2 | representative offset/width/reset | `nsm.rd.cfg_1.axi_rresp [1:0]=0x2`; `nsm.rd.error_data_0 [31:0]=0xdeadbeef`; `tpb_xt_local_reg.q7.release_run_stall [7:0]=0xFF` | byte-exact ✓ |
| 3 | BundleSizeInBytes hex-vs-decimal | `udma_m2s.AXI_M2S_MLA BSIB="256"` (dec=`0x100`) vs `nsm.control BSIB="0x100"` | radix split confirmed ✓ (hex-read 598 overstrides) |
| 4 | per-instance stride / symbolic ArraySize | `notific_nq ArraySize="NUM_SW_Q"` → `Parameters[NUM_SW_Q]=10`, stride `0x28` | symbolic resolution ✓ |
| 5 | block→schema→address join | `intc_4grp.ctrl ArraySize="INTC_NUM_GROUPS"=4`, stride `0x40`, AO bare `0`; `TPB_0_POOL_LOCAL_REG@0x2803060000 + 0x3000 = 0x2803063000` | join + decimal-AO ✓ |

Supporting checks: `udma_s2m SizeInBytes="98304"` (= `0x18000`) + `IF=NONE`; `gpio/pvt SizeInBytes`
decimal `"4096"`/`"65536"`; `rdm_model queue_size_0 AddressOffset="32"` (= `0x20`, decimal);
`xtensa_q7 AddrWidth=14 SizeInBytes=0x4000 IF=APB`. All 20 `../control/csr/*.md` cross-links and
the `../control/address/{block-schema-xref,unified-soc-memory-map}.md` links confirmed resolvable
on disk.

**`[HIGH · OBSERVED]`** — every offset/width/reset/access row above (re-read from the schema JSON);
the 20-page count and the page→schema map; the radix taxonomy; the symbolic-ArraySize resolution;
the block→schema→address join math.
**`[MED · INFERRED]`** — the SoC-absolute *leaf bases* (carried from the ADDR-lane pages, family-level
not per-leaf-grepped here); the per-gen "additive vs restructured" readings.
**`[INFERRED]`** — all Maverick / NC-v5 *interior* behavior (the JSON is header-OBSERVED only; no v5
register schema ships for most blocks; the byte-grounded behavioral reference is the Cayman schema).

> **NOTE.** Nothing here is fabricated and no vendor source snapshot was consulted. Every offset,
> width, reset, and binding traces to the shipped `cayman-arch-regs` CSR JSON / `arch-headers` copies,
> parsed this session (lawful interoperability reverse engineering, DMCA 17 U.S.C. §1201(f)).

---

## See also

- The 20 narrative CSR pages (§2 index) — the *why* behind every row here.
- [`block-schema-xref.md`](../control/address/block-schema-xref.md) — the block→schema bind (the join's hop 1→2).
- [`unified-soc-memory-map.md`](../control/address/unified-soc-memory-map.md) — the SoC-absolute region table (hop 2→3).
- [`soc-master-map.md`](../control/address/soc-master-map.md) — the full 126-root / 34,858-node chip map.
- [`master-capability-matrix.md`](../generations/master-capability-matrix.md) — the per-gen CSR applicability of every block.
