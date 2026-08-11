# CSR — UDMA_GEN + GEN_EX + TDMA_MODEL

The M2S ([udma-m2s.md](udma-m2s.md)) and S2M ([udma-s2m.md](udma-s2m.md))
descriptor engines do not own the whole SDMA channel. They are the two
data-moving halves; **everything they share** — design revision, AXI-master
common config, the cross-DMA mailbox, the interrupt-aggregator address tables,
the per-queue AXI-error masks, the SR-IOV virtual-machine mapping — lives in two
*common* register files (`udma_gen`, `udma_gen_ex`) and one *application-glue*
block (`tdma_model`). This page documents those three.

All three are shipped as binary-derived CSR register schemas under the Cayman
arch-regs tarball (`csrs/sdma/{udma_gen,udma_gen_ex,tdma_model}.json`). Every
count, offset, bit-range, reset, and access type below is read from the
JSON and cross-checked against `output/address_map/address_map_flat.yaml`.
The page default is `[HIGH/OBSERVED]`; claims that depart from it carry an
explicit `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` tag.

> Schema shape (all three): `RegFile.RegistersBundleArrays[]` is an array of
> *bundle arrays*, each with `Name`, `AddressOffset` (relative to the regfile
> base), `ArraySize`, `BundleSizeInBytes`, and `Registers[]`. Each register has
> a `Name`, an `AddressOffset` **relative to its bundle base**, an `AccessType`,
> and `BitFields[]`. Absolute byte offset of register *r* in instance *i* of a
> bundle is `bundle.AddressOffset + i*BundleSizeInBytes + r.AddressOffset`.

---

## 1. The three blocks at a glance

| block | window | regfile base in channel | bundles | reg-defs | bitfield-defs | reg-instances | role |
|---|---|---|---|---|---|---|---|
| `udma_gen`    | 0x4000 (16 KiB) | `+0x38000` | 22 | 69 | 203 | 2956 | common control: revision, AXI-master cfg, mailbox, int-aggregation, AXI-error, coarse VMID, PMU, mem-init |
| `udma_gen_ex` | 0x4000 (16 KiB) | `+0x3C000` | 5  | 29 | 68  | 284  | V4 virtualization: per-queue VMPR, section/transaction-type routing (BUFF1/BUFF2/DDP/drop) |
| `tdma_model`  | 0x1000 (4 KiB)  | `+0x40000` (node `MISC_SDMA_APP`) | 8 | 44 | 59 | 148 | SDMA application glue: event-accel doorbell, RD/WR reorder buffers, broadcast cfg, notification triggers, app-engine inflight status |

Counts:

```
                  udma_gen   udma_gen_ex   tdma_model
  bundle arrays      22           5            8
  register defs      69          29           44
  bitfield defs     203          68           59
  register insts   2956         284          148   (sum reg*ArraySize)
  reg AccessType   RO11 RW55 WO3  RO1 RW28    RO4 RW40
  bf  AccessType   RO79 RW109 WO15  RO15 RW53  RO11 RW48
  DataWidth          32           32           32
  AddrWidth        14 bit       14 bit       12 bit
  InterfaceType      APB          APB          APB
```

`register-instances` multiplies by `ArraySize`; the `udma_gen` figure (2956) is
dominated by `int_ctrl_base_addr` (ArraySize 2048 — a per-interrupt-source
address table), so 2956 is *table entries*, not 2956 distinct programmable
registers.

> **Schema gotcha — `BundleSizeInBytes` encoding differs.** In `udma_gen` and
> `udma_gen_ex` the per-register `AddressOffset` is a hex string (`"0x..."`) but
> `BundleSizeInBytes` is **decimal** (`DMA_misc`=256, `VMPR`=64, `Mailbox`=64).
> In `tdma_model` **both** `BundleSizeInBytes` and `SizeInBytes` are **hex
> strings** (`"0x0200"`, `"0x18"`, `"0x1000"`). Do not apply the decimal rule to
> `tdma_model`.

> **Schema gotcha — `tdma_model` bundle order is not address-monotonic.**
> `app_engine_status` (`+0x600`) is listed in the JSON *after* `notific_cfg`
> (`+0x700`) but sits *below* it in address space. Sort by `AddressOffset`, not
> array index.

> **CORRECTION — instance layout.** An earlier anchor placed
> `GEN@+0x78000 / GEN_EX@+0x7C000`. The Cayman address map
> (`address_map_flat.yaml`, node `APB_SE_0_SDMA_0_*`, OBSERVED verbatim) places
> the **unicast** channel as `GEN@+0x38000 / GEN_EX@+0x3C000`. The `+0x78000`
> band is in the **broadcast mirror** (`BCAST_UDMA_GEN @ +0xB8000` within the
> `+0x80000` BCAST region — not `+0x78000` either). The byte-grounded unicast
> placement is used throughout this page; see section 2.

---

## 2. How the five blocks compose into one SDMA channel

One SDMA channel is a contiguous `0x40000` UDMA window plus a `0x4000` MISC
window. Sample channel `APB_SE_0_SDMA_0` from `address_map_flat.yaml` (offsets
relative to the channel base, OBSERVED):

| offset in channel | node | size | backing JSON |
|---|---|---|---|
| `+0x00000` | `UDMA_M2S`      | `0x20000` | `udma_m2s.json` |
| `+0x20000` | `UDMA_S2M`      | `0x18000` | `udma_s2m.json` |
| `+0x38000` | **`UDMA_GEN`**     | `0x04000` | `udma_gen.json` |
| `+0x3C000` | **`UDMA_GEN_EX`**  | `0x04000` | `udma_gen_ex.json` |
| `+0x40000` | **`MISC_SDMA_APP`** | `0x01000` | `tdma_model.json` |
| `+0x41000` | `MISC_NOTIFIC` | `0x01000` | `notific_10_queue.json` |
| `+0x80000` | `BCAST_UDMA_*` mirror | `0x40000` | (M2S/S2M/GEN/GEN_EX) |

Composition facts:

- **GEN/GEN_EX are the COMMON half both engines share.** A single `udma_gen` +
  `udma_gen_ex` pair serves both M2S and S2M of a channel — proven by fields
  that carry *both* a `tx_*` (M2S) and an `rx_*` (S2M) control in one register:
  `AXI.endian_cfg` has `swap_m2s_*` **and** `swap_s2m_*`; `VMADDR.cfg_vm_ctrl`
  has `tx_q_vmid_*` **and** `rx_q_vmid_*`; `axi_outstanding_cnt` has `tx_*`
  **and** `rx_*` counters; `axi_queue.cfg` has `tx_mask_*` **and** `rx_mask_*`.
- S2M ends at `+0x38000` (`+0x20000 + 0x18000`), so GEN begins exactly where S2M
  ends — the channel does not reserve a gap between S2M and GEN here.
- `tdma_model` is **not in the UDMA window** — it is node `MISC_SDMA_APP` in the
  MISC window at `+0x40000`, immediately below `MISC_NOTIFIC` (`+0x41000`). That
  adjacency is why its `notific_cfg*` triggers route into the
  `notific_10_queue` block.

**Instance multiplicity across the SoC** (`rg -c` on `address_map_flat.yaml`):

```
  udma_m2s.json     280 nodes
  udma_s2m.json     280 nodes
  udma_gen.json     280 nodes  (264 UNICAST + 16 BCAST)
  udma_gen_ex.json  280 nodes  (264 UNICAST + 16 BCAST)
  tdma_model.json   264 nodes  (264 UNICAST + 0 BCAST)   <-- no broadcast copy
```

GEN/GEN_EX get a 16-node broadcast mirror because the broadcast M2S/S2M still
need their shared control surface; `tdma_model` has no mirror (264 == the
unicast UDMA count, 280-16) because the app-glue / notification / app-engine
plumbing is only meaningful on real unicast channels, not on a fan-out broadcast
writer. `[HIGH/OBSERVED counts; MED/INFERRED rationale]`

---

## 3. `udma_gen` — the shared control surface

### 3.1 Bundle map (22 arrays, `BundleSizeInBytes` DECIMAL)

| base | end | arr | bs | #reg | bundle / meaning |
|---|---|---|---|---|---|
| `0x00000` | `0x02000` | 2048 | 4 | 1 | `int_ctrl_base_addr` — per-interrupt-source address table (2048x32-bit) |
| `0x02000` | `0x02100` | 64 | 4 | 1 | `int_ctrl_sec_addr` — secondary address table |
| `0x02100` | `0x02200` | 1 | 256 | 9 | `DMA_misc` — `int_cfg` / `revision` / `general_cfg_1..5` (incl. timer) / `abort_ctl` / `bw_bug_fixes` |
| `0x02200` | `0x02300` | 4 | 64 | 3 | `Mailbox[4]` — cross-DMA `Interrupt` / `msg_out` / `msg_in` |
| `0x02300` | `0x02400` | 1 | 256 | 3 | `AXI` — `cfg_1` (timeout) / `cfg_2` (arb promotion) / `endian_cfg` |
| `0x02400` | `0x02464` | 25 | 4 | 1 | `SRAM_ctrl[25]` — read/write margin timing |
| `0x02500` | `0x02504` | 1 | 4 | 1 | `vmid` — `cfg_vmid_0` (MSI-X VMID select/enable) |
| `0x02600` | `0x02700` | 16 | 16 | 3 | `VMADDR[16]` — `cfg_vm_ctrl` / `cfg_tx_vm` / `cfg_rx_vm` (coarse per-queue VMID) |
| `0x02700` | `0x028c0` | 16 | 28 | 7 | `axi_outstanding_cnt[16]` — tx/rx desc/data/cmpl outstanding + reset strobes |
| `0x02900` | `0x02ac0` | 7 | 64 | 16 | `axi_error_detection_table[7]` — `addr0..15` range classifiers |
| `0x02b00` | `0x02b38` | 7 | 8 | 2 | `axi_error_control[7]` — `table_addr` / `table_data` |
| `0x02c00` | `0x02cc0` | 16 | 12 | 3 | `axi_queue[16]` — `state_request` / `error_status` / `cfg` |
| `0x02d00` | `0x03400` | 64 | 4 | 1 | `int_ctrl0..6` — 7x64-entry interrupt-aggregator group register files |
| `0x03400` | `0x03410` | 1 | 16 | 4 | `pmu` — rx_hdr / rx_data / tx_data / rx_drop thresholds |
| `0x03420` | `0x03440` | 1 | 32 | 4 | `init_memory` — rx/tx `init_trigger` / `init_in_progress` |
| `0x03460` | `0x03470` | 1 | 16 | 4 | `spare_reg` — zeroes0/1, ones0/1 |

Max bundle end = `0x3470`, window `0x4000` -> no overlap, `0xb90` headroom. The
seven `int_ctrl0..6` bundles share one row above; each is a separate 64x32-bit
file.

### 3.2 Design revision — the only version surface in the GEN family

`DMA_misc.revision` @ bundle `+0x4` (abs `0x02104`), `RO`:

| bits | field | reset | meaning |
|---|---|---|---|
| `[31:24]` | `major_id` | `0x1c` | design major revision ID |
| `[23:12]` | `minor_id` | `0x360` | design minor revision ID |
| `[11:0]` | `programming_id` | `0x03` | programming-interface revision ID |

This is the **only** version/revision register in the whole gen family —
`udma_gen_ex` deliberately has none (section 4.3).

### 3.3 The AXI-master common config + arbitration

> **CORRECTION — there is no per-queue "arbitration weight" register and no
> "DMA mux".** The full arbitration/scheduling surface in `udma_gen` is exactly:
> (a) one global AXI-master priority-promotion control,
> (b) one MSI-X AXI-QoS field, and
> (c) per-queue *scheduling-stop / mark-packet* masks in `axi_queue.cfg`.
> A `jq` sweep of every bitfield name+description for `arb\|mux\|weight\|prio\|qos\|schedul`
> returns only those. M2S-vs-S2M ordering is not weight-arbitrated in this block;
> M2S and S2M are independent engines whose outbound AXI traffic shares the
> single AXI master that `AXI.cfg_2.arb_promotion` governs.

`AXI` bundle (`BundleSizeInBytes`=256, one instance):

| reg | off | bits | field | acc | reset | meaning |
|---|---|---|---|---|---|---|
| `cfg_1` | `+0x00` | `[31:0]` | `tout` | RW | `0x02710` | timeout (in cycles) for **all** AXI transactions |
| `cfg_2` | `+0x04` | `[3:0]`  | `arb_promotion` | RW | `0x8` | increment this AXI master's priority after this many arbitration cycles (0 = disabled) |
| `endian_cfg` | `+0x08` | `[4]` | `swap_64b_en` | RW | `0x0` | 0 = swap 4-byte groups, 1 = swap 8-byte groups |
| | | `[3]` | `swap_s2m_data` | RW | `0x0` | swap S2M data write |
| | | `[2]` | `swap_s2m_desc` | RW | `0x0` | swap S2M descriptor read + completion write |
| | | `[1]` | `swap_m2s_data` | RW | `0x0` | swap M2S data read |
| | | `[0]` | `swap_m2s_desc` | RW | `0x0` | swap M2S descriptor read + completion write |

`DMA_misc.int_cfg` @ `+0x00`: `[6:4] msix_axi_qos` (RW, MSI-X AXI QoS),
`[0] msix_64` (RW, 1 = 64-bit MSI-X message, 0 = 32-bit).

Per-queue AXI-error scheduling masks live in `axi_queue[16].cfg` @ `+0x08` —
these are the closest thing to per-queue scheduling control:

| bits | field | acc | meaning |
|---|---|---|---|
| `[21]` | `tx_mask_stream_error` | WO | mask error indication to APP on AXI stream |
| `[20]` | `tx_mask_cmpl_error` | RW | mask M2S completion error on non-active queue |
| `[19]` | `tx_mask_stop_sched` | RW | when set, packet **will be** scheduled (mask the stop) |
| `[18]` | `tx_mask_stop_desc_prefetch` | RW | when set, prefetch **will** occur (mask the stop) |
| `[17:16]` | `tx_mark_pkt` | RW (rst `0x1`) | 0=never / 1=mark if NON_ACTIVE / 2=mark any |
| `[12]` | `tx_state` | RO | per-queue AXI-error state (0=ACTIVE 1=NON_ACTIVE) |
| `[8]` | `rx_mask_cmpl_error` | RW | mask S2M completion error on non-active queue |
| `[7]` | `rx_mask_drop_stream` | RW | when set, packet **will not** be dropped |
| `[6]` | `rx_mask_stop_desc_prefetch` | RW | when set, prefetch **will** occur |
| `[5:4]` | `rx_mark_pkt` | RW (rst `0x1`) | as `tx_mark_pkt` for the RX/CMPL side |
| `[0]` | `rx_state` | RO | per-queue AXI-error state |

### 3.4 Cross-DMA mailbox + interrupt aggregation

`Mailbox[4]` (`BundleSizeInBytes`=64) is a cross-DMA doorbell:

| reg | off | field | acc | meaning |
|---|---|---|---|---|
| `Interrupt` | `+0x00` | `[0] set` | WO | generate an interrupt to **another** DMA engine |
| `msg_out` | `+0x04` | `[31:0] data` | RW | 32-bit outgoing message slot |
| `msg_in` | `+0x08` | `[31:0] data` | RO | 32-bit incoming message slot |

Interrupt aggregation: `int_ctrl_base_addr[2048]` + `int_ctrl_sec_addr[64]` are
per-source MSI-X/interrupt address tables; `int_ctrl0..6` are seven 64-entry
aggregator-group register files. `DMA_misc.abort_ctl` @ `+0x1c` masks abort
indications: `[1] mask_app_abort` (toward APP/Adapter), `[0] mask_local_abort`
(from the local INT controller).

`axi_outstanding_cnt[16]` exposes live AXI back-pressure counters per queue —
`tx_desc_read`, `tx_data_read`, `tx_cmpl_write`, `rx_desc_read`,
`rx_data_write`, `rx_cmpl_write` (each `[31:24] value`, RO) and a WO
`rst_counters` strobe register. `pmu` carries occupancy `inuse_high_th` /
`inuse_low_th` thresholds for rx_hdr/rx_data/tx_data plus per-queue rx_drop
counts. `init_memory` is the desc/cmpl SRAM init handshake (write `init_trigger`,
poll `init_in_progress`).

### 3.5 Programming the AXI master + arbitration (C pseudocode)

```c
/* gen_base = channel_base + 0x38000  (the udma_gen regfile)                  */
/* Bundle bases (relative to gen_base), from section 3.1:                     */
#define GEN_DMA_MISC   0x02100u   /* int_cfg +0x00, revision +0x04, ...        */
#define GEN_AXI        0x02300u   /* cfg_1 +0x00, cfg_2 +0x04, endian +0x08    */
#define GEN_AXI_QUEUE  0x02c00u   /* per-queue, stride BundleSizeInBytes = 12  */

static inline void udma_gen_program_axi_master(volatile uint32_t *gen_base) {
    volatile uint32_t *axi = (volatile uint32_t *)((uintptr_t)gen_base + GEN_AXI);

    axi[0] = 0x00002710u;          /* cfg_1.tout  — AXI transaction timeout     */

    /* cfg_2.arb_promotion[3:0]: bump this master's AXI arb priority after N    */
    /* arbitration cycles (0 disables promotion). This is the ONLY AXI-master  */
    /* arbitration knob in udma_gen — there are no per-queue weights.          */
    axi[1] = (axi[1] & ~0xFu) | (8u & 0xFu);

    /* endian_cfg: M2S read vs S2M write byte-swap, independently per stream.  */
    /* swap_64b_en[4]=0 -> 4-byte groups; leave all swaps off for LE host.     */
    axi[2] = 0x0u;
}

/* Per-queue AXI-error scheduling masks live in axi_queue[q].cfg (+0x08).      */
static inline void udma_gen_unmask_queue_sched(volatile uint32_t *gen_base, unsigned q) {
    volatile uint32_t *cfg =
        (volatile uint32_t *)((uintptr_t)gen_base + GEN_AXI_QUEUE + q*12u + 0x08u);
    /* clear tx_mask_stop_sched[19] / tx_mask_stop_desc_prefetch[18] so a      */
    /* non-active queue actually stops, per the schema's inverted "mask" sense.*/
    *cfg &= ~((1u << 19) | (1u << 18));
}
```

---

## 4. `udma_gen_ex` — the V4 virtualization extension

### 4.1 Bundle map (5 arrays, `BundleSizeInBytes` DECIMAL)

| base | end | arr | bs | #reg | bundle / meaning |
|---|---|---|---|---|---|
| `0x000` | `0x400` | 16 | 64 | 13 | `VMPR[16]` — per-queue virtual-machine registers (V1-V3) |
| `0x400` | `0x800` | 16 | 64 | 4  | `VMPR_V4[16]` — V4 high-address SELECTOR registers |
| `0x800` | `0x810` | 1  | 16 | 3  | `section_ctrl` — section feature + drop-address remap |
| `0x820` | `0x828` | 1  | 8  | 2  | `init_transaction_table` — `init_trig` / `init_in_progress` |
| `0xc00` | `0xc1c` | 1  | 28 | 7  | `transaction_type_table` — per-transaction routing / VMID |

Max bundle end `0xc1c`, window `0x4000` -> no overlap.

### 4.2 `VMPR[16]` — fine-grained per-queue TX+RX virtual-memory mapping

Each queue gets 13 registers. The TX (M2S) and RX (S2M) sides are fully split,
and the RX side independently maps a 2-buffer scatter (`BUFF1`/`BUFF2`) plus a
DDP (Direct Data Placement) target. Selected fields (all reset `0x0`):

| reg | off | key fields |
|---|---|---|
| `cfg_vmpr_0` | `+0x00` | `[29] tx_q_cmpl_vmid_en` / `[28] tx_q_pref_vmid_en` / `[7] tx_q_data_vmid_en` / `[5:0] tx_q_hisel` (marked "Not available from V4") |
| `cfg_vmpr_1` | `+0x04` | `[31:0] tx_q_vmaddr_hi` |
| `cfg_vmpr_2` | `+0x08` | `[31:16] tx_q_cmpl_vmid` / `[15:0] tx_q_pref_vmid` |
| `cfg_vmpr_3` | `+0x0c` | `[31:16] tx_q_data_vmid_sel` / `[15:0] tx_q_data_vmid` |
| `cfg_vmpr_4` | `+0x10` | RX map: `[29] rx_q_cmpl_vmid_en` / `[28] rx_q_pref_vmid_en` / `[27:24] rx_q_buf2_msb_addr_sel` / `[23] rx_q_ddp_vmid_en` / `[21:16] rx_q_ddp_hisel` / `[15] rx_q_buf2_vmid_en` / `[13:8] rx_q_buf2_hisel` / `[7] rx_q_buf1_vmid_en` / `[5:0] rx_q_buf1_hisel` |
| `cfg_vmpr_5` | `+0x14` | `[31:0] rx_q_buf2_msb_addr` (BUF2 independent MSB address word) |
| `cfg_vmpr_6..9` | `+0x18..+0x24` | RX cmpl/pref/buf1/buf2/ddp VMID + select pairs |
| `cfg_vmpr_10..12` | `+0x28..+0x30` | `rx_q_buf1_vmaddr_hi` / `rx_q_buf2_vmaddr_hi` / `rx_q_ddp_vmaddr_hi` |

`VMPR_V4[16]` (4 regs) replaces the per-stream `hisel` fields with explicit
high-address selectors: `cfg_vmpr_v4_0 tx_q_vmaddr_hi_sel`,
`v4_1 rx_q_buf1_vmaddr_hi_sel`, `v4_2 rx_q_buf2_vmaddr_hi_sel`,
`v4_3 rx_q_ddp_vmaddr_hi_sel` (all RW, reset `0x0`).

`section_ctrl` + `transaction_type_table` add S2M scatter/section routing absent
from `udma_gen`:

| reg | field | meaning |
|---|---|---|
| `section_ctrl.cfg_v4_section_en` | `[2] allow_sec_addr_round_up` / `[1] remap_drop2ddp` / `[0] val` | enable section feature |
| `section_ctrl.drop_addr_lsb` | `[31:0] val` (rst `0x0`) | dropped-write target low word |
| `section_ctrl.drop_addr_msb` | `[31:0] val` (rst **`0xDEADBEEF`**) | dropped-write target high word — POISON default `[HIGH/OBSERVED]` |
| `transaction_type_table.addr` | `[8:5] queue` / `[4:0] transaction_type` | selects the table row to program |
| `transaction_type_table.write_cmd` | `[1:0] val` | **0=BUFF1, 1=BUFF2, 2=DDP, 3=drop** |
| `transaction_type_table.count_cmd` | `[2:1] field_selection` / `[0] calc_cmd` | which length to use (none/BUFF1/BUFF2) + override/accumulate |
| `transaction_type_table.axi_write_padding` | `[6:4] eos_addr_round_up_cmd` / `[3] eos_wstrb_cmd` / `[2] sos_wstrb_cmd` / `[1:0] padding_cmd` | section-end alignment + 64-B cache-pad |

### 4.3 GEN vs GEN_EX — what `_ex` actually adds

These are **two separate `0x4000` APB windows** (GEN @ `+0x38000`, GEN_EX @
`+0x3C000`), not "gen plus inline extra registers". The `_ex` suffix is **not**
a version bump.

**GEN_EX adds (present in `gen_ex`, absent in `gen`):**

- Per-queue `VMPR[16]` — fine-grained TX+RX VMID per prefetch/data/completion
  stream, 64-bit VM-address-high, RX 2-buffer + DDP independent address spaces.
  `udma_gen` only has the **coarse** `VMADDR[16]` (3 regs: one tx + one rx
  vmid/vmaddr pair) plus a global `vmid.cfg_vmid_0`. GEN_EX is the fine-grained
  successor of `gen.VMADDR`. `[HIGH/OBSERVED structure; MED/INFERRED "successor"]`
- `VMPR_V4[16]` high-address selectors (the V4 indirection that deprecates the
  `hisel` fields).
- `section_ctrl` + `transaction_type_table` — per-transaction routing to
  BUFF1/BUFF2/DDP/drop with section-end alignment and AXI write padding. No
  analog in `udma_gen`. `drop_addr_msb` defaults to the `0xDEADBEEF` poison.
- `init_transaction_table` — a separate mem-init handshake for the transaction
  table (distinct from `gen.init_memory`, which inits the desc/cmpl SRAMs).

**GEN_EX does NOT add (explicitly checked):**

- No version/revision/id register — a `jq` name sweep for `rev\|version` over
  `gen_ex` returns empty; the version surface stays in `udma_gen.DMA_misc.revision`.
- No descriptor-ring registers (those are in the M2S/S2M engines).
- No AXI common cfg / interrupt aggregation / mailbox / PMU (all GEN-only).

Net: **`gen` = common control + revision + int-aggregation + AXI-error + coarse
VMID; `gen_ex` = per-queue/per-transaction virtualization + scatter routing.**
They are complementary, never overlapping. `[HIGH/OBSERVED + MED/INFERRED]`

---

## 5. `tdma_model` — the SDMA application glue

### 5.1 What `tdma_model` is — and is NOT

`tdma_model` is **not** a descriptor engine and is **not** a tensor-DMA
descriptor ring. The entire file has **zero** ring base/head/tail pointers,
**zero** dimension/stride registers, **zero** `descriptor_offset` registers. It
is the SDMA *application glue* block (address-map node `MISC_SDMA_APP`) that:

1. accelerates event signalling (read-1 / read-0 doorbell polling),
2. owns the read/write **reorder buffers** (ROB/WOB — bundle `robert`),
3. configures **broadcast groups**,
4. programs all the **notification triggers** that feed the adjacent
   `notific_10_queue` block (`MISC_NOTIFIC`), and
5. exposes per-queue **inflight status** for the SDMA application engines
   (CME / DRE / CCE / ATS) plus a DMB barrier count.

**The actual tensor data-path operations are in sibling engine schemas in the
same `csrs/sdma/` directory; `tdma_model` only counts and routes them:**

- `dre.json` (Data Reordering Engine) — where the multi-dimensional strided
  model actually lives: a `custom_transpose[8]` `command` register with
  per-dimension (x/y/z/w) stride/row include bits, i.e. a 4-D strided DMA
  **transpose** descriptor with 8 in-flight command slots. **"Tensor DMA" in the
  device abstraction is DRE's strided transpose — there is no hardware block
  literally named "tdma".** `[HIGH for DRE fields; MED/INFERRED for the naming
  bridge]`
- `cce.json` (compute/collective engine) — FP32 FMA + an in-DMA decompression
  path; the reduce/decompress engine on the collective path.
- `cme.json` (single cfg reg) — the lightweight multi-packet command/merge engine.
- `ATS` (Address Translation Service) — no own JSON here; only counted by
  `app_engine_status.ats_packets_inflight`. `[MED/INFERRED]`

> **NOTE — corpus redaction artifact.** Some descriptions in the sibling schemas
> replace the token "ATS" / "address translation" with the placeholder token
> "il" (e.g. CME inflight "Packets currently in the il"). Treat "il" there as the
> redacted "ATS". This is a description artifact, not a schema error.
> `[MED/OBSERVED-artifact]`

**App-glue role (the bridge).** `tdma_model` is the per-channel
observability + notification + reorder layer that sits *around* the data path
`TPB engine (PE/POOL/ACT) <-> SDMA app engines (DRE/CCE/CME/ATS) <-> udma_m2s/s2m <-> AXI/HBM`.
The device tensor-DMA abstraction maps onto raw UDMA as follows: a device-level
tensor op is decomposed into a DRE transpose / CCE reduce command plus a UDMA
descriptor ring program; the engine's packets flow through `udma_m2s`/`udma_s2m`;
and `tdma_model` (a) counts those in-flight packets per queue
(`app_engine_status`), (b) fires a notification into `notific_10_queue` when a
programmed packet-count threshold is hit (`notific_cfg_per_queue`), and (c) keeps
AXI responses ordered across the reorder buffer (`robert`). The only hard binding
tying this block to DRE/CCE/CME/ATS is the `app_engine_status` inflight bitmaps;
the PE/POOL/ACT binding is indirect (via the app-engine inflight + the
notification queue), not register-level here. `[HIGH/OBSERVED for the bindings
present; MED/INFERRED for the end-to-end mapping]`

### 5.2 Bundle map (8 arrays, `BundleSizeInBytes` HEX STRINGS)

Sorted by address (the JSON array order is non-monotonic — see section 1 gotcha):

| base | end | arr | bs | #reg | bundle / meaning |
|---|---|---|---|---|---|
| `0x000` | `0x200` | 1 | `0x200` | 11 | `tdma` — event-accel + ROB/WOB cfg + broadcast cfg |
| `0x200` | `0x380` | 16 | `0x18` | 6 | `notific_cfg_per_queue[16]` — M2S/S2M trigger / CRC / sw-queue |
| `0x500` | `0x560` | 2 | `0x30` | 12 | `notific_cfg_fabric_trigger[2]` — AXI req/rsp + APB-write match |
| `0x600` | `0x624` | 1 | `0x24` | 5 | `app_engine_status` — CME/DRE/CCE/ATS/DMB inflight |
| `0x700` | `0x724` | 1 | `0x24` | 3 | `notific_cfg` — stall / s2m_eop_combined / timestamp |
| `0x800` | `0x880` | 1 | `0x80` | 4 | `robert` — ROB backpressure + outstanding counters |
| `0x900` | `0x910` | 2 | `0x8`  | 2 | `spares` — spare_zeros / spare_ones |
| `0x980` | `0x984` | 1 | `0x4`  | 1 | `strongly_ordered_write` — cfg |

Max bundle end `0x984`, window `0x1000` -> no overlap.

### 5.3 Key registers (byte-exact)

`tdma` bundle (event accel + reorder buffers + broadcast):

| reg | off | key fields |
|---|---|---|
| `tdma_event_accel` | `+0x00` | `[0] en` (rst `0x0`) — enables both accel addr pairs |
| `tdma_set_event_accel_cfg_0/1` | `+0x04/+0x08` | read-1 doorbell address lo/hi (rst `0x4` / `0x90`) |
| `tdma_clr_event_accel_cfg_0/1` | `+0x0c/+0x10` | read-0 doorbell address lo/hi (rst `0x0` / `0x90`) |
| `tdma_stat_0/1` | `+0x14/+0x18` | `num_set` / `num_clr` accelerated events (RO) |
| `tdma_rob_cfg` | `+0x1c` | `[31] powerdown` / `[12:4] rid_base` / `[3] use_rid_base` / `[2] clear` / `[1] force_inorder` / `[0] en` — **read** reorder buffer |
| `tdma_wob_cfg` | `+0x20` | `[10:4] wid_base` / `[3] use_wid_base` / `[2] clear` / `[1] force_inorder` / `[0] en` — **write** reorder buffer |
| `broadcast_cfg_group` | `+0x100` | `[15:0] group` (rst `0xffff`) — bit *i*=1: this SDMA is in broadcast group *i* |
| `broadcast_cfg_last_node` | `+0x104` | `[15:0] last_node` (rst `0x0`) — bit *i*=1: this SDMA is the last node of group *i* |

`notific_cfg_per_queue[16]` (stride `0x18`) — per-queue packet-count triggers
into the notification queue:

| reg | off | fields |
|---|---|---|
| `m2s_trigger` | `+0x00` | `[16] trigger_en` / `[15:0] trigger_cnt` (packets/queue before re-trigger; 0 = every packet) |
| `m2s_crc_init` | `+0x04` | `[31:0] value` (rst `0xffffffff`) |
| `m2s_sw_queue` | `+0x08` | `[3:0] id` (target SW notification queue 0..15) |
| `s2m_trigger` / `s2m_crc_init` / `s2m_sw_queue` | `+0x0c/+0x10/+0x14` | as M2S, for the S2M side |

`notific_cfg_fabric_trigger[2]` (stride `0x30`) adds AXI-read-request,
AXI-read-response, and APB-write address-match triggers (`*_id_mask` / `*_id_cmp`
/ `*_addr_mask` / `*_addr_cmp` + `*_sw_queue`). `notific_cfg.stall` (`+0x00`)
back-stalls the SDMA<->APP path when the notification block is not ready
(`m2s_sop_eop_en`, `s2m_sop_eop_en`, `s2m_cmpl_en`, all rst `0x1`).
`notific_cfg.timestamp` (`+0x20`) carries a `[23:0] delta` (rst `0x400`) added
to the timestamp per cycle.

`app_engine_status` — per-queue inflight bitmaps (one bit per queue, `[15:0]`):

| reg | off | acc | meaning |
|---|---|---|---|
| `cme_packets_inflight` | `+0x00` | RW | CME packets in flight, per queue |
| `dre_packets_inflight` | `+0x04` | RW | DRE (transpose) packets in flight |
| `cce_packets_inflight` | `+0x08` | RW | CCE (compute/decompress) packets in flight |
| `ats_packets_inflight` | `+0x0c` | RW | ATS (address-translation) packets in flight |
| `dmb_packets_inflight` | `+0x20` | RW | packets carrying a DMB / write barrier |

> **In-place clarification — `app_engine_status` access type.** These rows are
> commonly formatted as RO. The Cayman JSON
> marks all five `app_engine_status` registers **RW** (verified: the only four RO
> registers in `tdma_model` are `tdma_stat_0`, `tdma_stat_1`,
> `robert.data_requests_outstanding`, `robert.responses_cached`). The RW access
> is consistent with the `RO4 RW40` register histogram. Treat them as readable
> status with a writable shadow (test/clear).

`robert` (the read/write reorder buffer back-pressure accounting):

| reg | off | acc | fields |
|---|---|---|---|
| `txdf` | `+0x00` | RW | `[9:0] overhead_beats_outstanding` (rst `0x14`) — beats counted when generating back-pressure |
| `data_requests_outstanding` | `+0x10` | RO | `[9:0] count` |
| `descriptor_requests_outstanding` | `+0x14` | RW | `[9:0] m2s_count` / `[25:16] s2m_count` |
| `responses_cached` | `+0x18` | RO | `[9:0] count` |

`strongly_ordered_write.cfg` (`+0x00`): `[0] bypass_enable`, `[4] clear`. The
two `spares` registers are RW (`spare_zeros` rst `0x0`, `spare_ones` rst
`0xFFFFFFFF`).

---

## 6. Cross-generation divergence (Cayman authoritative)

Cayman (`cayman-arch-regs`) vs `sunda` / `mariana` / `mariana_plus`
(customop-lib arch-headers), via normalized `jq -S` comparison:

- **`udma_gen.json`** — Cayman == Mariana == Mariana_plus (byte-identical
  normalized). Cayman **!=** Sunda:
  - `DMA_misc.revision`: Cayman `major=0x1c minor=0x360 prog=0x03`; **Sunda
    `major=0x02 minor=0x01 prog=0x01`** — Cayman is a much later silicon revision
    of the same IP. (Verified directly.)
  - `axi_outstanding_cnt.rst_counters`: Cayman WO; Sunda RW.
  - A few `axi_queue`/other field access types WO/RO->RW in Sunda.
  - `pmu.tx_data` widths narrower in Sunda (`[26:16]hi/[10:0]lo` vs Cayman
    `[28:16]hi/[12:0]lo`).
- **`udma_gen_ex.json`** — Cayman == Mariana == Mariana_plus == Sunda
  (byte-identical, 5 bundles / 29 regs everywhere). The V4 virtualization
  extension is **stable across all four gens**. (Verified: 5/29 each.)
- **`tdma_model.json`** — the fastest-evolving of the three:
  - **Sunda: 6 bundles** (minimal — `tdma`, `notific_cfg`,
    `notific_cfg_fabric_trigger`, `notific_cfg_per_queue`, `app_engine_status`,
    `spares`).
  - **Cayman: 8 bundles** (Sunda set + `robert` (ROB) + `strongly_ordered_write`).
  - **Mariana / Mariana_plus: 19 bundles** (Cayman-class + `rob` (== Cayman's
    `robert`, renamed), `rob_latency_bin_lower/upper`, `new_wob_cfg`,
    `demux_ack_fifo_ctrl`, `embedded_semaphore_update`/`_ctrl`,
    `tdg_ragged_and_emb_sem_ctrl`, `misc_udma_qos_ctrl`, `udma_qos_ctrl`,
    `udma_timeout_ctrl`, `sdma_perf_debug`). (Verified: 6 / 8 / 19 / 19.)

  Cayman is the intermediate step: it adds the reorder buffer and
  strongly-ordered-write over the Sunda baseline; Mariana adds the
  embedded-semaphore / QoS / timeout / perf-debug machinery.

> **Per-gen applicability NOTE.** All Cayman (NC-v3) figures above are byte-grounded
> from the schemas. Sunda (NC-v2) figures are byte-grounded from its own headers.
> A **v5** generation is **not present** in this corpus — any v5 extrapolation
> would be **INFERRED** and is not asserted here. `[v5: INFERRED/absent]`

---

## See also

- [CSR — UDMA_M2S](udma-m2s.md) — the outbound (memory->stream) descriptor engine
- [CSR — UDMA_S2M](udma-s2m.md) — the inbound (stream->memory) descriptor engine
- [UDMA hardware engine](../../dma/udma-hw-engine.md) — the engine model GEN/GEN_EX sit under
- [DGE builder QoS / arbitration](../../dma/dge-builder-qos.md) — SDMA QoS and descriptor-generation scheduling
- [SDMA address windows (APB)](../../dma/sdma-windows-apb.md) — the channel address windows reconciled in section 2
- [PKL-DMA subtree](../address/pkl-dma-subtree.md) — the DMA address subtree placement
