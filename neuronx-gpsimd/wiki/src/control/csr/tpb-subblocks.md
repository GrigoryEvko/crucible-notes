# CSR — TPB Sub-Blocks (SBUF / events / act-table / DGE)

> **Scope.** This page is the *remainder sweep* of the TPB CSR tree
> (`csrs/tpb/`) on **Cayman (NC-v3)** — everything **not** already covered by the
> three dedicated TPB CSR pages:
> [`tpb_xt_local_reg`](tpb-xt-local-reg.md) (the Q7-core face), [`tpb.json` top
> cluster control](tpb.md), and [`tpb_arr_seq_top_host_visible` — the PE-array
> sequencer](pe-array-sequencer.md). What is left, and what this page documents
> byte-exact, is **six** regfiles plus one cross-tree ECC block:
>
> | block | file | role |
> | --- | --- | --- |
> | SBUF cluster control | `csrs/tpb/tpb_sbuf_cluster.json` | 2-stage SBUF port arbiter + TDM + AXI→SRAM transpose |
> | SBUF per-engine throttlers | `csrs/tpb/tpb_sbuf_pool_act.json` | POOL/ACT/DVE read/write token-bucket QoS |
> | per-bank ECC/parity | `csrs/erg/erg_parity_model.json` | the SBUF/PSUM-bank parity/ECC + BIST-init face (**`csrs/erg/`, not `csrs/tpb/`**) |
> | events/semaphores | `csrs/tpb/tpb_events_semaphores_axi.json` | 256 events + 256 semaphores, 5 op windows |
> | PE-array cluster seq | `csrs/tpb/tpb_arr_seq_cluster_host_visible.json` | per-cluster matmul config + 36-rowgroup perf |
> | PE-array protected seq | `csrs/tpb/tpb_arr_seq_top_protected.json` | privileged 9-lane XBUS throttle |
> | HW activity monitor | `csrs/tpb/tpb_ham.json` | closed-loop PE-array power/di-dt throttle manager |
>
> All offsets, widths, bit positions, access types, and reset values are
> **byte-exact** from the shipped Cayman register-description schema
> (`InterfaceType APB`, `DataWidth 32`, `RegfileFlavor POSEDGE`). The page default
> is `[HIGH/OBSERVED]`; claims that depart from it carry an explicit
> **HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED** tag. Cayman (v3) is
> byte-grounded; cross-gen counts (v2 sunda / v4 mariana family / v5 maverick) are
> JSON-grounded; v5-*interior* semantics are flagged **INFERRED** where the header
> is the only OBSERVED anchor.
>
> **What is NOT here, and why.** The task title lists "act-table" and "DGE" — both
> resolve to *non-regfiles* under static analysis: the activation tables are
> **memory regions** in the ACT engine (no bitfield regfile — [§8](#8-activation-tables--no-regfile-act-memory-regions)),
> and the DGE is **descriptor-RAM driven** (no CSR of its own — [§9](#9-dge--no-regfile-descriptor-ram--udma-ring)).
> The per-engine PE/POOL/ACT/DVE/SP *control* is **not** a standalone block either;
> it is reconciled to the `tpb.json` sequencer bundles + the Q7 run-state CSRs in
> [§7](#7-per-engine-control--no-standalone-block).

---

## 0. Directory coverage ledger and schema shape

The Cayman `csrs/tpb/` directory contains exactly **10 `.json` regfiles** (+ one
`.mako` generator). The split between the prior pages and this page:

| file | `SizeInBytes` | bundles / regs | covered by |
| --- | --- | --- | --- |
| `tpb_xt_local_reg.json` | `0x...` | — | [tpb-xt-local-reg](tpb-xt-local-reg.md) |
| `tpb_arr_seq_top_host_visible.json` | `0x1000` | — | [pe-array-sequencer](pe-array-sequencer.md) |
| `tpb.json` | `0x1000` | 9 bundles | [tpb (top cluster control)](tpb.md) |
| `tpb_sbuf_cluster.json` | `0x1000` | 5 / 10 | **§1** |
| `tpb_sbuf_pool_act.json` | `0x1000` | 4 / 16 | **§2** |
| `tpb_events_semaphores_axi.json` | `0x100000` | 5 / 5 | **§4** |
| `tpb_arr_seq_cluster_host_visible.json` | `0x1000` | 3 / 17 | **§5** |
| `tpb_arr_seq_top_protected.json` | `0x1000` | 1 / 1 | **§6** |
| `tpb_ham.json` | `0x1000` | 7 / 30 | **§3** |

Counts above come from each JSON's own `RegistersBundleArrays`.

**Common schema shape.** Every file is `{ "RegFile": {...} }` with
scalar metadata (`UnitName`, `DataWidth="32"`, `AddrWidth`, `SizeInBytes`,
`InterfaceType`, `Type="REGFILE"`, `RegfileFlavor="POSEDGE"`). The register tree
is `RegistersBundleArrays[]` → `{ Name, AddressOffset, BundleSizeInBytes,
ArraySize, Registers[] }`; each register is `{ Name, AddressOffset, AccessType,
BitFields[] }`; each `BitField` is `{ Name, Position("hi:lo" or single bit),
AccessType, ResetValue, Description }`. **`BundleSizeInBytes` is the per-element
stride** — a bundle's total span = `BundleSizeInBytes × ArraySize`.

> **GOTCHA — register `AccessType` ≠ bitfield `AccessType`.** The register-level
> `AccessType` is almost always `RW`; the *real* read/write/read-only semantics
> live on the `BitField`. In `tpb_events_semaphores_axi` the register node says
> `RW` while the bitfield says `RO`/`WO` per window. Always read access from the
> bitfield.

> **GOTCHA — mixed `ResetValue` formatting.** `tpb_events_semaphores_axi` uses a
> bare `"0"` (no `0x`) for every reset value; the other five batch files use
> `"0x.."`. Do not assume a uniform encoding when parsing. All `AddressOffset` /
> `BundleSizeInBytes` / `SizeInBytes` are hex everywhere.

> **GOTCHA — name vs bus type.** `tpb_events_semaphores_axi.json` declares
> `InterfaceType = "APB"` (`DataWidth 32`, `AddrWidth 20`) despite the `_axi`
> filename suffix. The filename describes the *fabric port*, not the regfile's CSR
> interface.

---

## 1. `tpb_sbuf_cluster` — SBUF port arbiter + TDM + AXI→SRAM transpose

The host-visible control plane for **one SBUF cluster**: a **two-stage port
arbiter** that grants the SBUF read/write ports among the matmul (PE-array)
clients and the pool/act read/write clients, a **TDM slot allocator**, and the
**AXI→SRAM transpose enable**. `AddrWidth 12` (4 KiB window), 5 bundles, 10 regs.
The 32 MiB / 128-partition SBUF geometry it governs is documented in
[SBUF/PSUM banks](../../dma/sbuf-psum-banks.md); this regfile is **instantiated at
cluster boundaries**, not once per bank.

```
bundle           off     stride  arr  regs
arb_stage1       0x000   0x100   1    arb_prio, arb_weight
arb_stage2       0x100   0x100   1    arb_prio, arb_weight
tdm_config       0x300   0x100   1    tdm_cfg
axi2sram_config  0x400   0x100   1    axi2sram_transpose_en
spare_regs       0x500   0x100   1    spare_register0..3
```

### 1.1 Two-stage arbiter

**Stage 1** arbitrates among the **5 PE-array (matmul) clients**; **stage 2**
arbitrates the stage-1 winner against the **4 pool/act read/write clients**. Both
stages carry a 3-bit strict priority and a 4-bit round-robin weight per client.

`arb_stage1.arb_prio @0x000` RW — strict priority, 5 PE-array clients

| bits | field | rst |
| --- | --- | --- |
| `[2:0]` | `pe_client_0` | `0x0` |
| `[6:4]` | `pe_client_1` | `0x0` |
| `[10:8]` | `pe_client_2` | `0x0` |
| `[14:12]` | `pe_client_3` | `0x0` |
| `[18:16]` | `pe_client_4` | `0x0` |

`arb_stage1.arb_weight @0x004` RW — round-robin weight, **4 bits each, all rst `0x1`**
(`pe_client_0 [3:0]` … `pe_client_4 [19:16]`).

`arb_stage2.arb_prio @0x100` RW — strict priority, second stage

| bits | field | rst |
| --- | --- | --- |
| `[2:0]` | `pool_rd_client` | `0x0` |
| `[6:4]` | `act_rd_client` | `0x0` |
| `[10:8]` | `pool_wr_client` | `0x0` |
| `[14:12]` | `act_wr_client` | `0x0` |
| `[18:16]` | `pe_rd_client` | **`0x1`** |

`arb_stage2.arb_weight @0x104` RW — same 5 clients, all rst `0x1`.

> **Reset asymmetry — the matmul read port wins by default.** Every stage-2
> priority resets to `0x0` **except** `pe_rd_client`, which resets to `0x1`. The
> stage-1-winning matmul read therefore enters stage 2 with a built-in priority
> edge over the pool/act streaming ports. This is the static counterpart to the
> §2 token-bucket QoS — it keeps a streaming POOL/ACT engine from starving the
> systolic array's SBUF read fan-in. `[HIGH/OBSERVED for the reset values; "wins
> by default" framing INFERRED-strong.]`

### 1.2 TDM and transpose

`tdm_config.tdm_cfg @0x300` RW

| bits | field | rst | meaning |
| --- | --- | --- | --- |
| `[15:0]` | `tdm_max_slots` | `0x8` | total TDM slots in the cycle |
| `[31:16]` | `tdm_dma_slots` | `0x1` | slots reserved for DMA (must be ≤ `tdm_max_slots`) |

`axi2sram_config.axi2sram_transpose_en @0x400` RW — `[0] transpose_en` rst **`0x1`**.

> **NOTE — "the transpose engine" is one enable bit.** The `axi2sram` transpose
> referenced by the SBUF bank model is a **single-bit mode flag on the AXI→SRAM
> bridge** (default ON), not a separate DMA transpose engine. It selects whether
> the bridge transposes the partition/free axes as data crosses from the AXI
> aperture into SRAM. `[HIGH/OBSERVED for the bit; engine framing INFERRED.]`

`spare_regs.spare_register0..3 @0x500/4/8/C` RW `[31:0]` — metal-ECO spares;
`sr0/sr1` rst `0x0`, **`sr2/sr3` rst `0xffffffff`** (default-to-one ECO straps).

> **CROSS-GEN.** The `axi2sram_config` bundle gains three registers on the
> mariana family (NC-v4) — **`raw_config`, `raw_conv`, `misc_config`** — bumping
> the file to **5 bundles / 13 regs** (RAW-format + convolution SBUF support not
> present in Cayman). **Sunda (NC-v2) drops the entire `axi2sram_config` bundle**
> (4 bundles / 9 regs — no AXI→SRAM transpose mode). Maverick (NC-v5) matches the
> mariana 5/13 shape. See the
> [capability matrix](../../generations/master-capability-matrix.md).

---

## 2. `tpb_sbuf_pool_act` — per-engine SBUF bandwidth throttlers

Independent **read and write token-bucket throttlers** for the three streaming
SBUF consumers — **POOL, ACT, DVE** — each "N transfers per window of M cycles".
4 bundles, 16 regs; the throttler template is identical across the three engines.

```
bundle          off     arr  regs
pool_throttler  0x000   1    rd_throttle_cfg0/1, wr_throttle_cfg0/1
act_throttler   0x100   1    (same 4)
dve_throttler   0x200   1    (same 4)
spare_regs      0x300   1    spare_register0..3
```

**Throttler template (identical in pool/act/dve):**

| reg @off | field | rst | meaning |
| --- | --- | --- | --- |
| `rd_throttle_cfg0 @+0x0` | `[0] disable_throttle` | `0x1` | **throttle OFF by default** |
| `rd_throttle_cfg1 @+0x4` | `[7:0] window_len` | `0x0` | clock cycles per window |
| | `[23:16] transfer_cnt` | `0x0` | transfers allowed per window |
| `wr_throttle_cfg0 @+0xC` | `[0] disable_throttle` | `0x1` | |
| `wr_throttle_cfg1 @+0x10` | `[7:0] window_len` / `[23:16] transfer_cnt` | `0x0` | |

Each engine's SBUF **read** and **write** port is independently rate-limited. The
default (`disable_throttle = 1` on every port) is fully open. This is the QoS knob
that keeps one streaming engine from starving the matmul read port arbitrated in
§1. `[HIGH/OBSERVED fields; QoS framing INFERRED.]`

> **There is no `pe_throttler` here.** The PE/matmul path is governed by the §1
> arbiter priorities and the §6 *protected* XBUS throttle — not a token bucket.
> POOL/ACT/DVE are the only three throttled SBUF consumers.

> **CROSS-GEN.** Sunda (NC-v2) is **3 bundles / 16 regs**: it **drops the
> `dve_throttler` bundle** but **adds a `*_throttle_cfg2` (rd+wr) pair** to each of
> `pool_throttler` and `act_throttler` (6 regs each), netting the same 16. So
> sunda has *no* DVE SBUF throttler but a richer pool/act throttler. The mariana
> family and maverick all match Cayman's 4/16.

---

## 3. `tpb_ham` — Hardware Activity Monitor (PE-array power/di-dt throttle)

`tpb_ham` is a **closed-loop power / di-dt governor** for the PE matmul array. It
**measures** array activity (X-Bus utilization or data-toggle count), **looks up**
a Monitor Action Table to map that activity to a `k/n` throttle ratio (data
allowed into the array `k` out of every `n` cycles), and **arbitrates** among HW,
SW, and GPIO monitors to select the final ratio fed to the array's input gate.
7 bundles, 30 regs.

> **DISAMBIGUATION — this `table` is NOT the activation table.** `tpb_ham` has a
> register named `tpb_ham_table` ("Monitor Action Table"). It maps PE-array
> *activity* → `{k,n}` *power-throttle* ratios. It is **completely unrelated** to
> the ACT engine's activation-function tables (§8), which map a function-id + input
> → piecewise-cubic coefficients. Same word, different subsystem.

```
bundle          off     stride  arr  role
tpb_ham_mon     0x000   0x40    4    4 HW activity monitors
tpb_ham_dam     0x200   0x10    1    data-activity edge/threshold config
tpb_ham_sw      0x300   0x20    2    2 SW-driven throttlers
tpb_ham_gpio    0x400   0x30    8    8 GPIO-triggered throttlers
tpb_ham_table   0x600   0x20    1    Monitor Action Table R/W port (4 tables × 16 entries)
tpb_ham_arb     0x800   0x14    1    monitor arbiter + winner + final ratio
tpb_ham_notifi  0x900   0x100   1    periodic / throttle notification queue
```

### 3.1 HW monitors (`tpb_ham_mon`, 4 instances)

`ctrl @+0x0` RW

| bits | field | rst | meaning |
| --- | --- | --- | --- |
| `[15]` | `util_sel` | `0` | `0`=current utilization, `1`=utilization **delta**, as action-table address |
| `[14]` | `cnt_sel` | `0` | `0`=data-activity count, `1`=X-Bus utilization |
| `[13:8]` | `meas_period_exp` | `5` | average over `2^m` cycles |
| `[7:2]` | `act_shift_amt` | `3` | right-shift to compress 8-bit activity → 3-bit table index (`8b >> 5 = 3b`) |
| `[1]` | `clear` | `0x0` | clear this monitor |
| `[0]` | `enable` | `0` | enable this monitor |

`ave_activity @+0x4` RO — cleared-on-read statistics: `[31:24] max` (rst `0x0`,
default-min), `[23:16] min` (rst **`0xFF`**, default-max), `[15:8] curr`,
`[7:0] prev`. `throttler @+0x14` RO — the selected `{k, n, k_div_n}` ratio plus
`[24] notific_en` / `[25] interrupt_en` (all rst `0`). `arb_won @+0x18` RO `[31:0]`
arbitration-win count (cleared on read). `test_activity @+0x24` RW —
`[31] override_en` forces the action-table address from `[3:0] addr` for test.

> **NOTE — `act_shift_amt` description math is for an 8-row util width.** The
> description derives `3` from "utilization width of 8 (0–128 rows/cycle) shifted
> to a 3-bit table index". The action table is therefore **8 entries** indexed by
> the top 3 bits of compressed activity.

### 3.2 Monitor Action Table (`tpb_ham_table`)

| reg @off | field | meaning |
| --- | --- | --- |
| `entry_rd @0x00` RO | `{k[7:0], n[15:8], k_div_n[23:16], notific_en[24], interrupt_en[25]}` | table-read result |
| `entry_wr @0x04` RW | same 5-field payload | table-write data |
| `ctrl @0x08` RW | `[3:0] entry_num`, `[6:5] table_num`, `[28] rd_en`, `[29] wr_en`, `[30] rd_ack` (RO), `[31] wr_ack` (RO) | R/W trigger + ack |
| `spare_0s @0x10` RW | `[31:0]` rst `0x0` | ECO |
| `spare_1s @0x14` RW | `[31:0]` rst **`0xffffffff`** | ECO |

`table_num` selects one of **four** action tables: `00`=Util short, `01`=Util med,
`10`=Util long, `11`=Data Activity. Each table is 16 entries (`entry_num [3:0]`),
mapping a measured-activity index → a `{k,n}` throttle ratio.

> **QUIRK — `rd_en`/`wr_en` name↔description swap in the shipped schema.** Bit
> `[28]` is *named* `rd_en` but its description reads "**Write** enable. Causes data
> in `entry_wr` to be written…", and bit `[29]` is *named* `wr_en` with description
> "**Read** enable. Causes data in `entry_rd` to be read…". The names and the
> descriptions contradict each other. A reimplementation must pick one convention
> and pin it — by the *descriptions* (which agree with the `entry_wr`/`entry_rd`
> data flow), `[28]` triggers the **write** and `[29]` triggers the **read**; by
> the *names* it is the reverse. Treat this as an un-resolved schema bug and verify
> against silicon before trusting either polarity.

### 3.3 SW / GPIO throttlers and the arbiter

`tpb_ham_sw` (2 instances): a `throttler` reg with the same `{k,n,k_div_n,
notific_en, interrupt_en}` payload (RW here), `notific_meta_lo/hi @0x04/0x08`,
`ctrl @0x10 [0] throttle_en`, and `arb_won @0x14`. `tpb_ham_gpio` (8 instances)
adds `status @0x10 [0] active` (RO) and a richer `ctrl @0x14`:
`[0] throttle_en`, `[1] sticky_sel` (1=pulse / 0=level detect), `[2] clear`.

`tpb_ham_arb` — the decision unit:

| reg @off | field | rst | meaning |
| --- | --- | --- | --- |
| `enable @0x00` | `[3:0] hw_mon` | `0x0` | enable the 4 HW monitors in arbitration |
| | `[5:4] sw_mon` | `0x0` | enable the 2 SW monitors |
| | `[13:6] gpio_mon` | `0x0` | enable the 8 GPIO monitors |
| | `[31] arb` | `0x0` | master enable; when clear, output defaults to 1:1 (no throttle) |
| `ctrl @0x04` | `[0] pn_en` | `0x0` | periodic-notification enable |
| | `[13:8] pn_period_exp` | `21` | average `k_div_n` over `2^m` cycles per periodic notify |
| | `[16] shadow_sel` | `0x0` | `1` = read stats from shadow regs (no clear-on-read) |
| `clear @0x08` | `[0] clear_all_stats` | `0x0` | write-1 clears all HAM stat counters |
| `throttler @0x0C` RO | `{k, n, k_div_n}` | **`0xFF`** each | the **final** ratio output to the array (rst = 1:1, no throttle) |
| `winner @0x10` RO | `[14:0] winner_vector` | **`0x4000`** | one-hot `{no_throttle, gpio[7:0], sw[1:0], hw[3:0]}` |

`winner` reset `0x4000` = bit 14 set = the `no_throttle` slot (the MSB of the
15-bit one-hot) — so at reset nothing throttles.

> **GOTCHA — clock-gated disable ordering.** The `arb @0x800[31]` field's own
> description warns that clearing it while HAM is active gates the clocks of
> several arbiter flops and can leave arbiter signals "stuck". The documented
> sequence is: disable the per-class enables (`hw_mon`/`sw_mon`/`gpio_mon`)
> **first**, wait ≥1 cycle for the arbiter flops to settle, *then* clear the master
> `arb` bit.

`tpb_ham_notifi.queue @0x900` RW — SW queue numbers + enables for HAM **periodic**
(`[3:0] ham_periodic`, `[16] ham_periodic_en`) and HAM **throttle**
(`[7:4] ham_throttle`, `[20] ham_throttle_en`) notifications (the descriptions tag
these "Pacific" — the notification-queue subsystem). `periodic_k_div_n_val @0x4`
and `periodic_util @0x8` (both RO) publish `{ave, max, min}` of the last periodic
measurement window.

> **HAM in the throttle hierarchy.** HAM is the **adaptive, measurement-driven**
> layer (di-dt / power). It complements the *static* QoS mechanisms: §1 arbiter
> (port priority), §2 token-bucket throttlers (SBUF bandwidth), and §6 protected
> XBUS throttle (privileged data-into-array). `[INFERRED from the four mechanisms'
> field semantics.]`

> **CROSS-GEN.** Cayman / mariana / maverick are all **7/30** with the identical
> bundle set. **Sunda (NC-v2) is 6/27** — it drops `tpb_ham_notifi` (no HAM
> notification queue). **Mariana_plus (NC-v4) is 8/34** — it inserts a
> `tpb_gradual_gpio_throttle` bundle (arr=2: `step_down_config`, `step_down_delay`,
> `step_up_config`, `step_up_delay`) between `tpb_ham_gpio` and `tpb_ham_table`
> for ramped (rather than step) GPIO throttling.

---

## 4. `tpb_events_semaphores_axi` — 256 events + 256 semaphores, 5 op windows

The on-chip cross-engine synchronization fabric: **256 single-bit hardware events**
+ **256 32-bit hardware semaphores**, the semaphores exposed through **four
operation-aliased address windows** (read / set / inc / dec). This is the
**data plane**; the absolute SoC placement and the barrier model are on the
[EVT_SEM address-map page](../address/evt-sem-regions.md), and the
**control plane** (threshold + notify enable) is the `events_semaphores` bundle in
[`tpb.json`](tpb.md) — see §4.2. `SizeInBytes 0x100000` (1 MiB), `AddrWidth 20`,
5 bundles each `ArraySize=256`, `BundleSizeInBytes=0x4`.

| bundle | off | op | access (bitfield) | field |
| --- | --- | --- | --- | --- |
| `tpb_events` | `0x0000` | set/clear bit | `RW` `[0]` | `value` — "event clear-0, event set-1" |
| `tpb_semaphores_read` | `0x1000` | read current | `RO` `[31:0]` | `value` — "Semaphore value" |
| `tpb_semaphores_set` | `0x1400` | overwrite | `WO` `[31:0]` | `value` — "Semaphore value" |
| `tpb_semaphores_inc` | `0x1800` | atomic `+=` | `WO` `[31:0]` | `value` — "value to increment semaphore by" |
| `tpb_semaphores_dec` | `0x1C00` | atomic `-=` | `WO` `[31:0]` | `value` — "value to decrement semaphore by" |

Each window's byte span is `256 × 4 = 0x400` (1 KiB); the four semaphore windows
pack contiguously `0x1000 → 0x2000`.

> **GOTCHA — the bitfield is named `value`, not `events`/`sems`.** The register
> nodes are named `events` / `semaphores`; the single bitfield inside each is named
> `value`. ResetValue is the bare `"0"` form (no `0x`) for this file only.

### 4.1 Same physical array, op-per-window

The four semaphore windows are **four address aliases of the same 256-entry
array**, distinguished by the *operation* the access performs:

```
write +0x1400[i]  →  sem[i]  = written            (set, WO)
write +0x1800[i]  →  sem[i] += written            (inc, WO)
write +0x1C00[i]  →  sem[i] -= written            (dec, WO)
read  +0x1000[i]  →  current sem[i]               (read, RO)
```

`[HIGH/INFERRED-strong — identical geometry + op-named windows]`

This block is programmed by **all five TPB engines** (the shared `Event_Semaphore`
opcode; engine enum `PE=0 / ACT=1 / POOL=2 / DVE=3 / TPB_SP=4`) — it is the
inter-engine barrier/handshake fabric. The NCFW barrier primitives map onto it:
`add_semaphore_inc` writes a peer's `+0x1800` window; the
`add_semaphore_wait_ge_and_dec` pattern reads `+0x1000` and writes `+0x1C00`.
`[HIGH/OBSERVED for the shared opcode + engine enum.]`

### 4.2 The control plane lives in `tpb.json`

The *data* array here has no threshold/notify logic. That is a separate 3-register
bundle, `events_semaphores @0x800` in [`tpb.json`](tpb.md):

| reg @off | field | rst | meaning |
| --- | --- | --- | --- |
| `sem_threshold_ctrl0 @0x0` | `[31:0] low` | `0x0` | semaphore low threshold |
| `sem_threshold_ctrl1 @0x4` | `[31:0] high` | `0xffffffff` | semaphore high threshold |
| `notific_ctrl @0x8` | `[0] notifications_en` | `0x0` | enable events/semaphore notifications |

So a reimplementation needs **both** files: `tpb_events_semaphores_axi` (the 1 MiB
counter array + op windows) and the `events_semaphores` bundle in `tpb.json` (the
threshold compare + notify enable that fires when a semaphore crosses
`[low, high]`).

> **CROSS-CHECK vs [evt-sem-regions](../address/evt-sem-regions.md) (#901) — byte-exact.**
> The address-map page independently derives the container at
> `TPB_0_EVT_SEM = 0x2802700000`, size `0x100000`, with the event window at
> off `0x0000` and the four semaphore windows at `0x1000 / 0x1400 / 0x1800 /
> 0x1C00`. The CSR schema confirms exactly 5 bundles at exactly those offsets. The
> CSR view is the relative-offset ground truth; the address-map page is the
> absolute SoC placement. **No divergence.**

> **CROSS-GEN.** `5/5/0x100000` is **identical** across cayman / mariana /
> mariana_plus / sunda, and **absent in maverick (NC-v5)** — the event/semaphore
> fabric is relocated/restructured under maverick's `tpb_top.json` reorg.

---

## 5. `tpb_arr_seq_cluster_host_visible` — PE-array cluster seq + per-rowgroup perf

The **per-cluster face** of the matmul array sequencer (companion to the
[top sequencer](pe-array-sequencer.md)): cluster-level config (array stagger,
response-pipe FIFO control, inter-instruction delay) plus **per-XBUS-row-group
(36 instances)** performance counters and read-response debug vectors. 3 bundles,
17 unique reg defs (the perf/debug bundles are 36-way arrayed).

```
bundle                off     stride  arr  regs
arr_seq_cfg           0x000   0x40    1    10  (cfg + perf-en + 4 spare + 4 stagger)
arr_seq_queue_perf    0x100   0x30    36   6   (3 × 64-bit lsb/msb counters / rowgroup)
arr_seq_rd_rsp_debug  0x900   0x4     36   1   (1 debug vector / rowgroup)
```

`arr_seq_cfg.arr_cluster_cfg @0x000` RW

| bits | field | rst | meaning |
| --- | --- | --- | --- |
| `[0]` | `enable_wl_last_active_col` | `0x0` | set last-active-column in the weight-load instr |
| `[4]` | `flush_p2f_fifos` | `0x0` | flush all p2f (PE→FIFO) FIFOs before next read response |
| `[8]` | `matmul_done_last` | `0x0` | `0`=done on **first** SBUF read response, `1`=done on **last** |
| `[12]` | `en_inter_instr_dly` | `0x0` | enable inter-instr response delay |
| `[21:16]` | `inter_instr_dly_cnt` | `0x8` | delay count (valid when `en_inter_instr_dly=1`) |
| `[26:24]` | `p2fifo_af` | `0x2` | p2-FIFO almost-full threshold |

> **QUIRK — `p2fifo_af` carries a copy-pasted description.** The shipped schema
> gives `[26:24] p2fifo_af` the *same* description string as `en_inter_instr_dly`
> ("add delay between instruction responses…"). By its field name and 3-bit width
> this is the **p2-FIFO almost-full threshold**, not a delay enable; the
> description is a documentation copy-paste artifact.

`arr_seq_cfg.perf_cntr_cfg @0x004` RW — `[0] cntr_en` (enable all `arr_seq_ififo`
counters), `[4] cntr_rst` (write-pulse, reset all perf counters). Four
`spare_register0..3 @0x08/C/10/14` (all rst `0x0`). Four
`array_stagger_rg0..3_ctrl1 @0x18/1C/20/24` — `[15:0] rgN_ctrl1` (max-limit for the
per-row-group idle-timer count), `[31:16] spare`.

`arr_seq_queue_perf` (36 instances, one per XBUS row group) — three 64-bit counters
each split lsb/msb, all RO: `matmul_active_cycle_cnt_lsb/msb @+0x0/+0x4`,
`wl_active_cycle_cnt_lsb/msb @+0x8/+0xC` (weight-load active cycles),
`idle_cycle_cnt_lsb/msb @+0x10/+0x14`. `arr_seq_rd_rsp_debug.debug_vector_0 @+0x0`
RO `[31:0]` — internal read-response-block signal observability, one per row group.

> **RECONCILIATION.** The **36** = 36 XBUS row-group instances of the systolic
> array. The [top sequencer page](pe-array-sequencer.md) reports the array's
> top-level instance/counter model; this cluster file adds the per-row-group
> breakdown. PSUM/precision control remain **absent** here (they live in other
> files).

> **CROSS-GEN.** Cayman / mariana / maverick are **3/17**. **Sunda (NC-v2) is
> 3/13** — it drops the four `array_stagger_rg0..3_ctrl1` regs, and its perf/debug
> bundles are **`arr=20`** (20 XBUS row groups, not 36). **Mariana_plus (NC-v4) is
> 3/21** — it adds four `array_stagger_allow_rg0..3_ctrl` registers to
> `arr_seq_cfg` (stagger-allow control; *not* the rampdown power-mgmt of §6).

---

## 6. `tpb_arr_seq_top_protected` — privileged 9-lane XBUS throttle

The **protected (privileged, non-host-visible)** face of the array sequencer. On
Cayman it is a **single register**: a per-XBUS throttle config. 1 bundle, 1 reg
(`SizeInBytes 0x1000` reserved, only `0x20` populated).

`arr_seq_cfg.throttle_cfg @0x000` RW

| bits | field | rst | meaning |
| --- | --- | --- | --- |
| `[8:0]` | `disable_throttle` | **`0x1ff`** | per-XBUS throttle disable (9 bits → 9 XBUS lanes; rst all-ones = all throttling OFF) |
| `[12:9]` | `hw_only_en_transfer_cnt_upd` | `0x0` | enable HW-controlled throttle (per XBUS) |

The 9-bit disable mask implies the matmul array has **9 XBUS throttle lanes**,
defaulting to throttling disabled. This is the privileged counterpart to the §2
token-bucket throttlers — but for the matmul/PE *data-into-array* path.
`[HIGH/OBSERVED for the fields; "9 XBUS lanes" INFERRED from the 9-bit width]`

> **CROSS-GEN — newer gens add adaptive array power rampdown.** Cayman **1/1** and
> sunda **1/1** are just `throttle_cfg`. **Mariana (NC-v4) is 1/5** and
> **mariana_plus / maverick are 1/12**, layering a full power-management rampdown
> feature onto the same `arr_seq_cfg` bundle: `idle_clk_gating`, `rampdown_enable`,
> `rampdown_count`, `rampdown_threshold`, and (mariana_plus/maverick)
> `rampdown_decrease`, `rampdown_busy_control`, `rampdown_busy_threshold`,
> `rampdown_busy_history`, `rampdown_auto_on`, `rampdown_auto_off`,
> `rampdown_auto_status`. The protected sequencer governs adaptive array power
> rampdown on those gens; Cayman does not.

---

## 7. Per-engine control — no standalone block

The PE/POOL/ACT/DVE/SP per-engine *control* is **not** a batch-C regfile. Under
static analysis it resolves to two existing homes:

**(a) The four data-path sequencer bundles live in [`tpb.json`](tpb.md), not here.**
`pe_sequencer @0x000` (6 regs), `pool_sequencer @0x100` (9 regs),
`act_sequencer @0x200` (18 regs), `dve_sequencer @0x300` (11 regs), each
`ArraySize 1`. These carry rounding mode, instruction debug, timestamp increment,
and special-value compare config — *datapath* config, not engine enable/PC.

**(b) There is no `sp_sequencer` bundle.** The engine enum includes `TPB_SP=4`, but
SP has no datapath sequencer bundle — its control is the **Q7-core run-state** set
(no `sp_sequencer` appears anywhere in `tpb.json`).

**(c) `run_state_0..7` lives in [`tpb_xt_local_reg`](tpb-xt-local-reg.md), not in
any engine bundle.** `release_run_stall` (holds all 8 Q7 cores at reset, rst
`0xFF`), `start_ctrl`, and `run_state_0..7` (one opaque status word per Q7
sequencer core) are Q7-core CSRs.

> **RECONCILIATION.** "Per-engine run-state" is realized at the **Xtensa-core**
> level (8 Q7 cores + per-NX), not at the datapath-engine level. The 8 Q7 cores
> *are* the programmable sequencer engines; `run_state_i` is core `i`'s published
> status. PC/start = the Xtensa `start_ctrl` + `start_addr` (xt_local_reg),
> run-state = `run_state_i`, config = the `tpb.json` sequencer bundles. There is no
> separate "engine enable/PC" CSR. `[INFERRED-strong from the union of
> xt_local_reg + tpb.json.]`

---

## 8. Activation tables — no regfile, ACT memory regions

There is **no activation-table CSR / regfile**. The ACT engine's four HW tables
(CONTROL / BUCKET / PROFILE / CAM) are **memory regions** in the ACT engine,
addressed as `TPB_0_ACT_{PROFILE_CAM, PROFILE_TABLE, BUCKET_TABLE, CONTROL_TABLE}`
(+ `*_LOCAL_STORAGE`) — RAM the firmware stages piecewise-cubic coefficients into,
not a POSEDGE regfile with bitfields. The element function is a CAM match → PROFILE
128 B entry → BUCKET 32 B `{d0, d1, d2, d3, x0}` cubic. See the
[activation/transcendental tables page](../../uarch/activation-transcendental-tables.md).
No act-table JSON exists anywhere in the `csrs/` tree.

The activation-function *select* path is in the instruction (the `Activate`
micro-op carries an `activation_func` byte → CAM id), and the ACT engine's CSR
surface is the `act_sequencer` bundle in `tpb.json` (its **special-value compare
bank** — `zero_val/mask`, `nan_val/mask`, `pos_inf_val/mask`, `neg_inf_val/mask`,
plus per-lane `rnd_mode` and stochastic-rounding control). So the ACT "CSR" =
`act_sequencer` config (regfile) + four ACT memory tables (RAM) — **no
activation-table regfile to extract.**

> **Do not conflate with §3.** The §3 `tpb_ham_table` "Monitor Action Table" maps
> *array activity → power-throttle ratio*; the ACT activation tables map *function
> + input → cubic coefficients*. Same word "table", entirely different subsystem.

---

## 9. DGE — no regfile, descriptor-RAM + UDMA ring

There is **no DGE (Descriptor Generation Engine) CSR / regfile** in `csrs/`
(verified by `rg` over the entire tree — the only "dge" hits are corrupted-text
false positives). The DGE is **descriptor-RAM driven**: it emits `REGWRITE`
descriptors into a UDMA ring, and the only CSRs it touches are the **UDMA ring
tail-increment** registers (the M2S / S2M descriptor-ring CSRs) plus the RDM.
`DGE_MEMORY` is the descriptor RAM (a memory region), not a regfile. So the "DGE
CSRs" resolve to: *(none of its own)* → UDMA ring CSRs + RDM. The DGE micro-op
encoding it stages into that RAM is documented in
[DGE micro-op encoding](../../dma/dge-microop-encoding.md).

---

## 10. The per-bank ECC / parity face — `erg_parity_model` (`csrs/erg/`)

The SBUF cluster's **ECC/parity + BIST-init** face is **not** in `csrs/tpb/` — it
is a separate regfile, `csrs/erg/erg_parity_model.json`, **instantiated once per
ECC bank** (16 SBUF nodes `SBUF_CLUSTER_{0..7}_{0,1}_ERG_CSR` + 32 PSUM nodes
`PSUM_CLUSTER{0..3}_ERG_CSR_{0..7}`, each a `0x40`-byte block at stride `0x40`).
`AddrWidth 6`, `SizeInBytes 0x40` (64 B), 1 bundle (`erg`), 10 regs. The
SBUF/PSUM banking that places these nodes is on the
[SBUF/PSUM banks page](../../dma/sbuf-psum-banks.md).

| reg @off | fields | role |
| --- | --- | --- |
| `init_cfg @0x0` | `[0] start` (init pulse), `[4] serialize`, `[8] init_type` (1=BIST/0=legacy), `[12] init_clk_en` | SRAM init / BIST kick |
| `init_status @0x4` RO | `[0] busy`, `[1] done`, `[2] unsupported` | init status |
| `mem_cfg @0x8` | `[0] shutdown`, `[4] rmea`, `[8] rmeb`, `[15:12] rma`, `[19:16] rmb`, `[20] test1a`, `[21] test1b`, `[22] testrnm`, `[23] testrwm` | bank power / repair / test |
| `cfg @0xc` | `[0] dis_uncerr`, `[11:4] eg_cfg0`, `[19:12] eg_cfg1` (error-gen), `[20] erg_type` (RO: **0=Parity, 1=ECC**) | ECC config |
| `eg_sram_uncerr @0x10` / `uncerr_sram_mask @0x14` / `uncerr_cnt @0x18` `[7:0]` (saturates `0xFF`) / `uncerr_sram_status @0x1c` / `uncerr_stat_clear @0x20` (WO) | per-SRAM uncorrectable-error gen-enable / mask / count / status / clear | uncerr path |
| `spare_reg @0x24` | `[31:0]` | metal ECO |

The `cfg.erg_type [20]` (RO) field is the **Parity-vs-ECC discriminator** for the
bank instance — `0` = parity, `1` = ECC. `mem_cfg` carries the SRAM **redundancy
MUX** repair selects (`rmea`/`rmeb` enables + `rma`/`rmb` 4-bit read-margin ports)
*and* the `shutdown` power-gate *and* the four self-timed/margin test pins.

> **CORRECTION vs [SBUF/PSUM banks](../../dma/sbuf-psum-banks.md) (#838) §3.2.** That
> page states that `mem_cfg` carries "only `shutdown` + the four
> `test1a/test1b/testrnm/testrwm` margin pins" and that the redundancy fields
> `rmea/rmeb/rma/rmb` "are **not** present in the shipped `erg_parity_model.json`".
> **For the Cayman (NC-v3) target this is incorrect** — the Cayman `mem_cfg` carries
> **9** fields *including* `rmea[4]`, `rmeb[8]`, `rma[15:12]`, `rmb[19:16]`. The
> divergence is **per-generation**, verified byte-exact across all five copies:
>
> | gen | `mem_cfg` fields |
> | --- | --- |
> | **cayman (v3)** | `shutdown, rmea, rmeb, rma, rmb, test1a, test1b, testrnm, testrwm` (9) |
> | **sunda (v2)** | `shutdown, rmea, rmeb, rma, rmb, test1a, test1b, testrnm, testrwm` (9) |
> | mariana (v4) | `shutdown, test1a, test1b, testrnm, testrwm` (5) |
> | mariana_plus (v4) | `shutdown, test1a, test1b, testrnm, testrwm` (5) |
> | maverick (v5) | `shutdown, test1a, test1b, testrnm, testrwm` (5) |
>
> #838's "not present" statement reads true for the **mariana-class** copy of the
> file, but the **Cayman and Sunda** copies *do* expose the redundancy MUX repair
> fields. A Cayman/Sunda control plane must program `rma/rmb/rmea/rmeb`; a
> mariana-class one must not (they are absent).

> **NOTE — parity, not ECC, by default reset.** `erg_type` resets read-only to
> the synthesized bank type; the filename (`erg_parity_model`) and the
> `0=Parity` encoding indicate the baseline SBUF banks are **parity** protected.
> The same regfile serves ECC banks when `erg_type` reads `1`.

---

## 11. Cross-generation summary (NC-v2 … NC-v5)

Every count below is `[HIGH/OBSERVED]`, read from each gen's `csrs/tpb/` +
`csrs/erg/` trees.
Cayman is the byte-grounded baseline; v5 *interior* semantics beyond the header are
**INFERRED** where flagged.

| block (bundles / regs) | sunda v2 | cayman v3 | mariana v4 | mariana_plus v4 | maverick v5 |
| --- | --- | --- | --- | --- | --- |
| `tpb_sbuf_cluster` | 4 / 9 (no axi2sram) | **5 / 10** | 5 / 13 (+raw/conv) | 5 / 13 | 5 / 13 |
| `tpb_sbuf_pool_act` | 3 / 16 (no dve, +cfg2) | **4 / 16** | 4 / 16 | 4 / 16 | 4 / 16 |
| `tpb_events_semaphores_axi` | 5 / 5 | **5 / 5** | 5 / 5 | 5 / 5 | **ABSENT** |
| `tpb_arr_seq_cluster_host_visible` | 3 / 13 (arr=20) | **3 / 17** | 3 / 17 | 3 / 21 (+stagger-allow) | 3 / 17 |
| `tpb_arr_seq_top_protected` | 1 / 1 | **1 / 1** | 1 / 5 (rampdown) | 1 / 12 | 1 / 12 |
| `tpb_ham` | 6 / 27 (no notifi) | **7 / 30** | 7 / 30 | 8 / 34 (+gradual gpio) | 7 / 30 |
| `tpb_misc_amzn` | ABSENT | ABSENT | 6 / 6 (wob) | 6 / 6 | **9 / 9 (wob + fab_remap)** |
| `tpb_dve` (own block) | — | — | — | — | 2 / 59 (config 38 + perf 21) |
| `erg_parity_model` `mem_cfg` | 9 fields | **9 fields** | 5 fields | 5 fields | 5 fields |

**Interpretation `[MED/INFERRED]`.** Cayman is the baseline. The mariana family
layers on RAW/conv SBUF support, gradual-throttle, WID-based write-ordering
(`tpb_misc_amzn`), and array power rampdown. Sunda is a *trimmed* variant (drops
transpose, HAM notify, DVE throttler, half the rowgroup perf counters) but **keeps**
the Cayman-class ECC redundancy fields. Maverick is a *structural reorg* (no
`tpb.json` → `tpb_top.json`; dedicated `tpb_dve` CSR block; no `events_sem`
regfile; `tpb_misc_amzn` gains three `tpb_fab_remap*` bundles).

> **CORRECTION — `tpb_misc_amzn` is not mariana-only.** It is often listed as
> "(mariana/mariana_plus only)". **Maverick (NC-v5) also ships `tpb_misc_amzn`** —
> as **9 bundles / 9 regs**: the six `*_wob` write-ordering bundles
> (`pe_seq_wob`, `dve_seq_wob`, `sp_seq_wob`, `aseq_wob`, `pseq_wob` + `q7_wob_misc`,
> each carrying `wob_wr_bypass[0]`, `wob_wr_clear[1]`, `wob_force_inorder[2]`,
> `wob_use_wid_base[3]`, `wob_wid_base[8:4]`) **plus three extra fabric-remap
> bundles** (`tpb_fab_remap`, `tpb_fab_remap_mask`, `tpb_fab_remap_local_bit`). It
> is absent only in cayman and sunda.

> **CORRECTION — naming mariana_plus's four extra cluster regs.** They are often
> given only as "(4 extra regs)". They are
> the four `array_stagger_allow_rg0..3_ctrl` registers (stagger-*allow* control) —
> distinct from the rampdown power-management registers, which belong to
> `tpb_arr_seq_top_protected` (§6), not the cluster file.

---

## 12. Reimplementation checklist (Cayman control plane)

1. **SBUF port arbitration** — program `tpb_sbuf_cluster` stage-1/stage-2
   priorities + weights; keep `pe_rd_client` ≥ pool/act if the matmul read port
   must not starve (it resets to `1` for this reason). Set `tdm_max_slots` /
   `tdm_dma_slots`; leave `axi2sram_transpose_en` = 1 unless feeding a
   non-transposed bridge.
2. **SBUF QoS** — `tpb_sbuf_pool_act`: throttling is OFF by default
   (`disable_throttle = 1` per port). To rate-limit POOL/ACT/DVE, clear
   `disable_throttle` and set `{window_len, transfer_cnt}`.
3. **ECC/parity** — per ECC bank, `erg_parity_model` (`csrs/erg/`): kick BIST/init
   via `init_cfg.start`, poll `init_status.done`; on Cayman/Sunda also program the
   `rma/rmb/rmea/rmeb` repair MUX in `mem_cfg`. `cfg.erg_type` reads the bank's
   parity/ECC mode.
4. **Sync fabric** — `tpb_events_semaphores_axi`: write the op windows (`set`
   `+0x1400`, `inc` `+0x1800`, `dec` `+0x1C00`); read `+0x1000`. Threshold/notify
   compare is in `tpb.json`'s `events_semaphores @0x800`.
5. **Matmul array** — `tpb_arr_seq_cluster_host_visible` for per-cluster config +
   perf; `tpb_arr_seq_top_protected.throttle_cfg` (privileged) for the 9-lane XBUS
   throttle.
6. **Power governor** — `tpb_ham`: program monitors + the four action tables,
   enable arbitration; **observe the clock-gated disable ordering** when tearing
   down `tpb_ham_arb.enable`.
7. **Engine sequencers** — PE/POOL/ACT/DVE config = `tpb.json` sequencer bundles;
   SP + run-state = Q7-core CSRs in `tpb_xt_local_reg`; activation tables = ACT
   memory regions; DGE = descriptor RAM + UDMA ring CSRs.

See the [master capability matrix](../../generations/master-capability-matrix.md)
for the per-generation CSR applicability of every block on this page.
