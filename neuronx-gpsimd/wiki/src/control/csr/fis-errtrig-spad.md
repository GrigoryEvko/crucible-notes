# CSR — FIS Control + errtrig + spad

This page documents three control surfaces that together form the per-FIS error and control
plane of a **Fabric Interface Slice (FIS)** — the slice the privileged on-die APB-IO fabric
inserts between every fabric master (an SDMA channel, a D2D / PCIe / TOP_SP block, …) and the
AXI fabric:

1. **`fis_control`** — the privileged per-FIS control regfile, plus its USER-side companion
   **`papb_bcast`** (the FIS APB-broadcast group mask: how one APB write fans out to many
   FIS instances);
2. **`errtrig`** — the error-trigger generator: a symmetric `intc_4grp` **PAIR**
   (`TRIG_0` / `TRIG_1`) plus the `notific_1_queue` whose status feeds it the 50-cause
   `fis_errtrig_intr` source vector;
3. **`spad`** — the scratchpad surfaces. There is **no `spad` CSR regfile**; the name resolves
   to the NCFW firmware `spad_ctrl` collective-config struct and the Maverick H-die hardware
   scratchpad SRAM.

Everything below is reconstructed byte-exact from the shipped Cayman register schema
(`csrs/fis/fis_control.json`, `csrs/fis/papb_bcast.json`, `csrs/intc/intc_4grp_{no_msix,msix}_unit.json`,
`csrs/notific/notific_1_queue.json`), the trigger YAMLs, the flat address map, and — for the
H-die scratchpad — the Maverick `arch-headers`. The errtrig `intc_4grp` PAIR primitive and the
abort-freeze controls are detailed in
[`../address/pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md) and
[`../interrupt/abort-scandump-clockstop.md`](../interrupt/abort-scandump-clockstop.md);
the routing of these triggers into the `peb_intc` apex is in the primary cross-link,
[`../interrupt/errtrig-fis-routing.md`](../interrupt/errtrig-fis-routing.md).

> **Scope / naming clarification `[HIGH · OBSERVED]`.** Only **one** of the three subjects is a
> discrete CSR JSON. `fis_control` (and `papb_bcast`) are real register files. `errtrig` is a
> *generator* — an address-map source leaf (`apb/intc_rdm/errtrig_{user,amzn}.yaml`) that
> instantiates two `intc_4grp` units + one notific queue; it has no register file of its own.
> `spad` is **not a register file at all** (`fd` for a `spad`/`scratchpad` CSR JSON across the
> whole tree returns zero). The "three blocks" the title names are therefore one regfile, one
> generator, and one firmware/hardware struct family.

---

## 1. The FIS container — where the three blocks live `[HIGH · OBSERVED]`

A FIS is a `0x10000` (64 KiB) container the fabric stamps once per master. Streaming the flat
address map for one representative master (`PEB_APB_IO_0_AMZN_SE_0_SDMA_0_FIS_0_*`) gives the
sub-region layout verbatim — the bases below are exact, the relative offsets are the low bytes:

| rel off | sub-region | size | backing `json` | block |
|--------:|------------|-----:|----------------|-------|
| `+0x0000` | `FIS_0_CTL` | `0x2000` | `csrs/fis/fis_control.json` | **§2 — `fis_control`** |
| `+0x2000` | `FIS_0_*_ERRTRIG_TRIG_0` | `0x1000` | `csrs/intc/intc_4grp_*_unit.json` | **§3 — errtrig PAIR half 0** |
| `+0x3000` | `FIS_0_*_ERRTRIG_TRIG_1` | `0x1000` | `csrs/intc/intc_4grp_*_unit.json` | **§3 — errtrig PAIR half 1** |
| `+0x4000` | `FIS_0_*_ERRTRIG_NOTIFIC` | `0x1000` | `csrs/notific/notific_1_queue.json` | **§3 — notific source** |
| `+0x5000` | `FIS_0_SPROT_AMZN_REMAPPER` | `0x1000` | `csrs/sprot/amzn_remapper.json` | [remapper.md](remapper.md) |
| `+0x6000` | `FIS_0_SPROT_QOS` | `0x1000` | `csrs/sprot/qos_prot.json` | [qos-prot.md](qos-prot.md) |

So the `0x3000` errtrig region is **exactly** `TRIG_0 @+0x2000 · TRIG_1 @+0x3000 · NOTIFIC
@+0x4000` (3 × `0x1000`). The sprot region (`amzn_remapper` + `qos_prot`, and the
`user_remapper` / `nsm` on the user side) is documented in
[remapper.md](remapper.md), [qos-prot.md](qos-prot.md), and [nsm.md](nsm.md).

> **Privilege split `[HIGH · OBSERVED]`.** `fis_control` is **AMZN-only** — all **582** physical
> `FIS_0_CTL` instances are under the `PEB_APB_IO_*_AMZN_*` (privileged) branch; **zero** appear
> on the USER side (`rg -c FIS_0_CTL` = 582). The USER FIS has *no* `fis_control`; its control
> surface is the cut-down `papb_bcast` (§2.9, **464** USER instances) plus the user-side sprot
> leaves (`user_remapper` + `qos_host_visible` + `qos_pmu`). `fis_control` is the privileged FIS
> controller; `papb_bcast` is what the user gets.

---

## 2. `fis_control` — the privileged FIS control regfile `[HIGH · OBSERVED]`

### 2.1 Regfile-level facts

| property | value | note |
|----------|-------|------|
| `UnitName` | `fis_control` | |
| `Type` | `REGFILE` | `RegfileFlavor` = `POSEDGE` |
| `DataWidth` | `32` | 32-bit APB data |
| `AddrWidth` | `13` | → `0x2000` (8 KiB) window |
| `SizeInBytes` | `"0x2000"` | **hex string** — the *only* hex value in the file (see GOTCHA) |
| `InterfaceType` | `APB` | |
| bundle arrays | **7** | `desc`, `axi`, `apb`, `apb_decode`, `sw_cntrl`, `iso_cntrl`, `apb_timeout` |
| register definitions | **36** | desc 6 · axi 4 · apb 2 · apb_decode 7 · sw_cntrl 11 · iso_cntrl 5 · apb_timeout 1 |
| register instances | **36** | every `ArraySize=1` — **no replicated bundles** |
| bitfield definitions | **49** | |
| field `AccessType` | RW 38 · RO 11 · WO 0 | no write-only fields |
| `SpecialAccess` | all `None` | no `PulseOnW` etc. anywhere in this file |
| `0xb1` reset placeholder | **absent** (`grep -ci 0xb1` = 0) | |

> **GOTCHA — DECIMAL `AddressOffset`s `[HIGH · OBSERVED]`.** Unlike its all-hex sprot siblings
> (`qos_prot`, `amzn_remapper`), `fis_control`'s bundle bases and register offsets are **decimal
> strings**. Proven by contiguity closure: the 7 bundle bases `0, 24, 40, 48, 76, 120, 140` are
> contiguous **only** under decimal (`24+16=40`, `40+8=48`, `48+28=76`, `76+44=120`, `120+20=140`,
> `140+4=144`), and `BundleSizeInBytes` `(24,16,8,28,44,20,4)` is likewise decimal. The whole reg
> map spans bytes `0..144` (`0x00..0x90`) inside the `0x2000` window — vast headroom. Read
> `perf_snapshot_lo_0 @28` as **decimal 28**, not `0x28`. The `SizeInBytes` window string is the
> lone hex exception.

### 2.2 Bundle map

| base (dec) | bundle | #reg | role |
|-----------:|--------|-----:|------|
| 0 | `desc` | 6 | FIS identity descriptor (RO topology) + 4 spares |
| 24 | `axi` | 4 | AXI gating request + outstanding-flushed quiesce status |
| 40 | `apb` | 2 | APB-side NTS gating + flush |
| 48 | `apb_decode` | 7 | two USER APB-decode windows (base/size/remap) + block-ID override |
| 76 | `sw_cntrl` | 11 | clock/reset enables, region enables, two 48-bit perf-snapshot windows |
| 120 | `iso_cntrl` | 5 | Pacific ↔ PCIE_A 4-phase reset/FLR/SBR/AXI-timeout handshake |
| 140 | `apb_timeout` | 1 | AMZN-chain EP APB watchdog |

### 2.3 `desc` — FIS identity (base 0)

| reg | +off | field [bits] | acc | rst | meaning |
|-----|-----:|--------------|-----|-----|---------|
| `die_id` | +0 | `die_id` [0] | RW | 0 | this FIS's die id |
| `fis_id` | +4 | `block_id` [8:0] | RO | 0 | this FIS's block ID |
| | | `bcast_block_id` [17:9] | RO | 0 | its **broadcast** block ID (the §2.9 target) |
| | | `num_fis_sprot` [25:20] | RO | 0 | # sprot sub-blocks in this FIS |
| | | `num_fis_nts` [31:26] | RO | 0 | # NTS sub-blocks in this FIS |
| `spare_zeros_0/1` | +8/+12 | `spares` [31:0] | RW | `0x0` | reset-to-0 spares |
| `spare_ones_0/1` | +16/+20 | `spares` [31:0] | RW | `0xffffffff` | reset-to-1 spares |

`fis_id` is a **read-only topology descriptor**: firmware reads it to learn this FIS's own
block ID, its broadcast ID, and the *count* of sprot + NTS sub-blocks it contains — i.e. how to
enumerate the FIS fan-out at runtime. `bcast_block_id` is the addressing key the APB-broadcast
fabric matches against (§2.9).

### 2.4 `axi` + `apb` — the quiesce surface (base 24 / 40)

| reg | +off | field [bits] | acc | rst | meaning |
|-----|-----:|--------------|-----|-----|---------|
| `axi/blk_en` | +0 | `valid` [0] | RW | 0 | FIS AXI block enable |
| `axi/sprot_gating` | +4 | `req` [31:0] | RW | 0 | per-sprot AXI gating request bitmask |
| `axi/nts_gating` | +8 | `req` [31:0] | RW | 0 | per-NTS AXI gating request bitmask |
| `axi/outstanding_flushed` | +12 | `sprot_combined` [0] | RO | 0 | all SPROTs' AXI outstanding drained |
| | | `nts_combined` [16] | RO | 0 | all NTSs' AXI outstanding drained |
| `apb/gating_req` | +0 | `nts` [0] | RW | 0 | gate APB-side NTS |
| `apb/outstanding_flushed` | +4 | `nts` [0] | RO | 0 | APB-side NTS drained |

The quiesce protocol: SW asserts `sprot_gating` / `nts_gating` (or `apb/gating_req`) to block new
transactions, then polls `outstanding_flushed.*_combined` until the FIS confirms drain. This is
the FIS-wide complement of `qos_prot`'s per-NTS `nts_isolation` ([qos-prot.md](qos-prot.md)).

> **QUIRK — schema typo preserved `[HIGH · OBSERVED]`.** `axi/outstanding_flushed`'s field
> descriptions ship the misspelling *"Combined **Outstadning** flushed signal from all SPROTs/NTSs
> in this FIS instance"* — quoted as-is; the field semantics are unaffected.

### 2.5 `apb_decode` — the two USER APB-decode windows (base 48)

| reg | +off | field [bits] | acc | rst | meaning |
|-----|-----:|--------------|-----|-----|---------|
| `user1_base` | +0 | `val` [31:0] | RW | 0 | USER region-1 base |
| `user1_size` | +4 | `val` [31:0] | RW | 0 | USER region-1 size |
| `user1_remap` | +8 | `val` [31:0] | RW | 0 | USER region-1 remap (target base) |
| `user2_base` | +12 | `val` [31:0] | RW | 0 | USER region-2 base |
| `user2_size` | +16 | `val` [31:0] | RW | 0 | USER region-2 size |
| `user2_remap` | +20 | `val` [31:0] | RW | 0 | USER region-2 remap |
| `user_fis_block_id_override` | +24 | `new_block_id` [9:1] | RW | 0 | replacement block ID |
| | | `en` [0] | RW | 0 | enable the block-ID override |

Two independently programmable USER APB **base/size/remap** windows + a block-ID override — the
APB-side address-decode/remap counterpart to the AXI-side `amzn_remapper` ([remapper.md](remapper.md)).
`apb_decode` holds only the **geometry**.

> **GOTCHA — the region *enables* are in a different bundle `[HIGH · OBSERVED]`.** `apb_decode`
> carries window geometry only. The `user1_en`/`user2_en` enables — and the `user_fis_en` /
> `user_debug_en` region gates — live in `sw_cntrl.apb_amzn_decode` / `sw_cntrl.apb_user_decode`
> (§2.6). Do not look for `user_fis_en` in `apb_decode`; it is not there.

### 2.6 `sw_cntrl` — clocks, resets, region gates, perf-snapshot (base 76)

| reg | +off | field [bits] | acc | rst | meaning |
|-----|-----:|--------------|-----|-----|---------|
| `clk_enable` | +0 | `en` [9:0] | RW | `0x3ff` | 10 per-sub-block clock enables — **all ON at reset** |
| `blk_reset` | +4 | `rst` [9:0] | RW | `0x000` | 10 per-sub-block resets — **all DEASSERTED at reset** |
| `fis_bh` | +8 | `en` [0] | RW | 0 | FIS bus-hold / black-hole enable |
| `fis_sprot_reset` | +12 | `rst` [31:0] | RW | 0 | SW reset of sprot sub-blocks (resets the qos counters) |
| `fis_nts_reset` | +16 | `rst` [31:0] | RW | 0 | SW reset of NTS sub-blocks |
| `apb_amzn_decode` | +20 | `user1_en` [0] · `user2_en` [8] | RW | 0 | AMZN-side region enables |
| `apb_user_decode` | +24 | `user1_en` [0] · `user2_en` [8] | RW | 0 | USER-side region enables |
| | | `user_fis_en` [16] | RW | **1** | **enables the USER FIS region** (where `qos_host_visible` lives) |
| | | `user_debug_en` [24] | RW | **1** | **enables the USER DEBUG_FIS region** (where `qos_pmu` lives) |
| `perf_snapshot_lo_0` | +28 | `count` [31:0] | RW | 0 | window-0 period low |
| `perf_snapshot_hi_0` | +32 | `count` [15:0] | RW | 0 | window-0 period high → **48-bit** window 0 |
| `perf_snapshot_lo_1` | +36 | `count` [31:0] | RW | 0 | window-1 period low |
| `perf_snapshot_hi_1` | +40 | `count` [15:0] | RW | 0 | window-1 period high → **48-bit** window 1 |

This is the gate/snapshot hub the FIS QoS observation triad depends on: `user_fis_en` and
`user_debug_en` are two **independent** region gates (both ON at reset, each SW-gateable); the two
`perf_snapshot{lo,hi}{0,1}` pairs are **48-bit** snapshot windows whose expiry freezes the
`qos_host_visible` mirror and advances the `qos_pmu` snap double-buffer
(`[MED · INFERRED]` cross-file stitch; the FIS reg facts are `[HIGH · OBSERVED]`).
`fis_sprot_reset` / `fis_nts_reset` reset the same sprot/NTS counters those mirrors expose.

### 2.7 `iso_cntrl` — Pacific ↔ PCIE_A reset handshake (base 120)

| reg | +off | field [bits] | acc | rst | meaning |
|-----|-----:|--------------|-----|-----|---------|
| `pacific_hs_peb_pcie_status` | +0 | `value` [15:0] | RW | 0 | status code to host; not part of the handshake (may change anytime) |
| `pacific_hs_peb_pcie_reset` | +4 | `req` [0] · `done` [1] | RW | 0 | Pacific needs / has completed a reset sequence |
| `pacific_hs_peb_pcie_axi_timeout` | +8 | `done` [0] | RW | 0 | Pacific finished AXI-timeout handling |
| `pacific_hs_peb_pcie_flr_sbr` | +12 | `done` [0] | RW | 0 | Pacific finished FLR/SBR handling |
| `pacific_hs_pcie_peb` | +16 | `reset_done_ack` [0] · `reset_ready` [1] · `flr_sbr_req` [2] · `axi_timeout` [3] | RO | 0 | inbound side (PCIE_A0 → Pacific) |

The **4-phase reset/isolation handshake** between the management core ("Pacific") and PCIE_A:
linkdown / FLR / SBR / AXI-timeout reset-sequence orchestration. The first four registers are the
Pacific→PCIE_A `req`/`done` outputs; `pacific_hs_pcie_peb` is the PCIE_A-driven RO inputs. This is
the FIS-resident backing for the PCIe isolation state machine
(`[MED · INFERRED]` tie to the NSM/isolation flow, [nsm.md](nsm.md)).

### 2.8 `apb_timeout` — the AMZN-chain APB watchdog (base 140)

| reg | +off | field [bits] | acc | rst | meaning |
|-----|-----:|--------------|-----|-----|---------|
| `apb_timeout/ctrl` | +0 | `limit` [31:0] | RW | **`0x2000`** | *"Limit for the APB timeouts on AMZN Chain EPs, 0=Disabled"* |

A free-running watchdog: a posted APB write that does not complete within `limit` cycles (default
**8192**) is declared a hung endpoint. `0` disables it. Its expiry is what produces the five
posted-write **SLVERR** conditions described next.

> **CORRECTION — the posted-write SLVERR latch is a Cayman→Mariana addition `[HIGH · OBSERVED]`.**
> In Cayman the five posted-write SLVERR conditions exist **only** as fire-and-forget HW interrupt
> triggers — `fis_cntrl_intr[0..4]` in the trigger YAMLs (`edge_triggered:true`, `needs_cdc:false`,
> e.g. *"AMZN chain AMZN EP posted write slave error"*) — with **no** cause/mask/status register in
> `fis_control`. Mariana / Mariana+ **promote** them into a first-class `fis_cntrl_intr` bundle
> (@base `256`/`0x100`, 4 regs: `mask` / `clr_on_read` / `status` / `apb_blk_error_addr`), turning a
> fire-and-forget trigger into a software-readable, maskable, address-capturing latch — see §5.

### 2.9 `papb_bcast` — the FIS APB-broadcast group mask `[HIGH · OBSERVED]`

`papb_bcast` is a tiny USER-side regfile (`AddrWidth 11` → `0x800`, APB, POSEDGE, **464** USER
instances) holding **one** register — the per-FIS broadcast-target mask:

| reg | +off | field [bits] | acc | rst | reg description (verbatim) |
|-----|-----:|--------------|-----|-----|----------------------------|
| `grps/mask` | +0 | `val` [15:0] | RW | **`0xfffe`** | *"16-bit mask for each of the 16 papb grps. Note grp0 is always non-bcast, so bit 0 is dont-care"* |

**The broadcast fan-out model.** A privileged APB write to a *broadcast aperture* is not addressed
to one FIS — the fabric replicates it to **every FIS whose broadcast-group bit is set in `mask`**.
The 16-bit `mask` is a per-FIS membership vector across **16 broadcast groups** (`papb grp0..grp15`):
bit *g* set means "this FIS participates in broadcast group *g*". A single CSR write to group *g*'s
aperture therefore lands atomically in all FIS instances that have bit *g* set — the mechanism by
which firmware programs N identical FIS slices (or N DMA engines behind them) with one write.

The reset value `0xfffe` is structurally exact: **bit 0 is clear** because the schema fixes
**`grp0` as the non-broadcast (unicast) group** — *"bit 0 is dont-care"* — and **bits 1..15 are
set**, so at reset every FIS is a member of all 15 *real* broadcast groups. Firmware narrows
membership by clearing bits.

> **NOTE — two distinct "broadcast" surfaces `[HIGH · OBSERVED / MED · INFERRED]`.** `papb_bcast`
> is the **CSR-level** per-FIS group-membership mask documented here. The NCFW runtime also drives a
> *separate* **DMA doorbell** broadcast — one write to a `BCAST_UDMA` window that the fabric fans to
> N DMA engines — covered in
> [`../../collectives/ncfw/dma-reprogram-apb-bcast.md`](../../collectives/ncfw/dma-reprogram-apb-bcast.md).
> They share the "one write → many targets" idea but are different apertures: `papb_bcast` is the
> CSR membership mask gating the privileged-APB broadcast fabric; `BCAST_UDMA` is the engine-facing
> doorbell. The group mask here is what selects which slices a broadcast reaches.

---

## 3. `errtrig` — the error-trigger generator `[HIGH · OBSERVED]`

### 3.1 It is a generator, not a regfile

`fd`/`rg` over `csrs/` for any `*errtrig*` register file returns **zero**. The errtrig block is an
address-map source leaf (`apb/intc_rdm/errtrig_{user,amzn}.yaml`) under the **INTC-RDM**
(interrupt-controller root-domain) that instantiates three sub-blocks, verbatim:

```
trig    count:2  ->  csrs/intc/intc_4grp_{msix|no_msix}_unit.json   (the PAIR)
notific          ->  csrs/notific/notific_1_queue.json
```

→ one errtrig = `TRIG_0 @+0x2000` + `TRIG_1 @+0x3000` + `NOTIFIC @+0x4000` = the `0x3000` ERRTRIG
region (§1). Every trigger domain on the die (SDMA / PCIe / FIS / IO-fabric / TPB / HBM / D2D / SP)
instantiates an errtrig, making it the most pervasive interrupt-aggregation primitive on the chip.

### 3.2 The USER/AMZN split == the MSIX/no_msix flavor split

Counting the flat address map (Cayman) by `json` binding and by name:

| census (Cayman flat-YAML) | count | binding |
|---------------------------|------:|---------|
| `intc_4grp_no_msix_unit.json` instances | **1070** | = 1068 `AMZN_ERRTRIG_TRIG_{0,1}` + 2 `PEB_INTC_TRIG_0` (apex) |
| `intc_4grp_msix_unit.json` instances | **858** | = 856 `USER_ERRTRIG_TRIG_{0,1}` + 2 `PEB_INTC_MSIX` (apex) |
| `notific_1_queue.json` instances | **962** | one per errtrig PAIR |
| `ERRTRIG_TRIG_0` (by name) | **962** | 428 USER + 534 AMZN |
| `ERRTRIG_TRIG_1` (by name) | **962** | 428 USER + 534 AMZN → **962 generator PAIRS** |

So: the **USER (host-reachable) errtrig takes the MSIX flavor** (delivers MSI-X straight to the
PCIe host); the **AMZN (on-die) errtrig takes the no_msix flavor** (emits the four severity
wire-ORs upward into the `peb_intc` apex). `TRIG_0 == TRIG_1 == 962` by construction — the pair is
symmetric, and the notific queue (962) is the same on both paths.

> **CORRECTION vs [`../address/pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md)
> (#908) `[HIGH · OBSERVED]`.** That page's Maverick-pkl headline (`TRIG_0 == TRIG_1 = 1,372`) is the
> **Maverick** generation count and stands. But its *Cayman cross-check column* lists **"642 pairs"**
> — which does **not** match the byte-grounded Cayman flat-YAML, where `TRIG_0`, `TRIG_1`, and
> `NOTIFIC` each count **962** (962 PAIRS), and the two `intc_4grp` schemas total **1928** units
> (1070 no_msix + 858 msix), or **1932** including the 4 RDM-root `intc_1grp_msix`. The "642" figure
> is inconsistent with `rg -c ERRTRIG_TRIG_0` = 962 on the same file; the Cayman pair count is
> **962**. The Maverick `1,372` is a *different SoC* and is not in conflict. Flagged here, not
> silently edited.

### 3.3 The PAIR latch/route structure

Each `intc_4grp` unit is `INTC_NUM_GROUPS = 4` groups × 32 bits = **128 trigger inputs**; the PAIR
(`TRIG_0` + `TRIG_1`) = **256-source capacity** — the "256-cap intc_rdm errtrig". Per-group
register file (12 regs, `arr = INTC_NUM_GROUPS`, stride `0x40`; absolute = `grp*0x40 + off`):

| reg | off | acc | rst | role |
|-----|-----|-----|-----|------|
| `int_cause_grp` | `0x00` | RW | `0x0` | the **latch** — HW sets per source; SW clears via **W0C** (Annapurna convention, **not** W1C) |
| `int_cause_set_grp` | `0x08` | WO | `0x0` | W1S software inject (auto-set on MSI-X ack if auto-mask) |
| `int_mask_grp` | `0x10` | RW | `0xffffffff` | per-bit mask — **all MASKED at reset** |
| `int_mask_clear_grp` | `0x18` | WO | `0x0` | W0C side-door to unmask |
| `int_status_grp` | `0x20` | RO | `0x0` | raw latched source status |
| `int_cdc_bypass_grp` | `0x24` | RW (no_msix) / **RO (msix)** | `0x0` | per-bit CDC edge-gen bypass |
| `int_control_grp` | `0x28` | RW | `0x1` | group control (moderation, clear-on-read, posedge, AWID…) |
| `int_error_msk_grp` | `0x2C` | RW | `0xffffffff` | Error = OR(Cause & !Error_Mask) |
| `int_abort_msk_grp` | `0x30` | RW (no_msix) / **RO (msix)** | `0xffffffff` | Abort = OR(Cause & !Abort_Mask) |
| `int_fatal_msk_grp` | `0x34` | RW | `0xffffffff` | Fatal = OR(Cause & !Fatal_Mask) |
| `int_log_msk_grp` | `0x38` | RW | `0xffffffff` | Log = OR(Cause & !Log_Mask) |
| `int_posedge_grp` | `0x3c` | RW | `0x0` | per-bit edge(1)/level(0) select |

The latch is `int_cause_grp` (HW-set, **W0C**-cleared — `[CITED · HIGH]` from the INTC-pair
derivation in [`../address/pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md), not
re-derived here; the `val` field's schema description is blank). The route is the **four severity
wire-ORs** (error / abort / fatal / log), each gated by its own per-bit mask, producing four
independent classified summary lines. The `int_cause_grp`, `int_cause_set_grp` and `int_mask_grp`
access types are identical across both flavors; the **diff** is exactly: `int_cdc_bypass_grp` and
`int_abort_msk_grp` go **RW → RO** in the MSIX flavor, and the `Sunda` bundle (@`0x300`) differs:

- **no_msix** `Sunda` (4 regs): `abort_cntl0`, `abort_cntl1` (the abort-freeze controls — each an
  8-bit-per-domain mask: `local_abort_scan_dump[7:0]`, `remote_abort_scan_dump[15:8]`,
  `local_abort_clock_stop[23:16]`, `remote_abort_clock_stop[31:24]`), plus two spares. These are
  the freeze actions the abort wire-OR drives — detailed in
  [`../interrupt/abort-scandump-clockstop.md`](../interrupt/abort-scandump-clockstop.md).
- **msix** `Sunda` (1 reg) + `PBA` (arr=4) + `VecTable` (arr=4) + `MSIX_Vector_Table_Space`
  (arr=`NUM_OF_TRIGS`): the MSI-X delivery apparatus instead of the abort-freeze block.

### 3.4 The 50-cause `fis_errtrig_intr` source vector `[HIGH · OBSERVED]`

The trigger YAMLs carry a `fis_errtrig_intr[0..49]` group = **50 entries**, all
`needs_cdc:false`, `edge_triggered:true`. It is a **mirror pair**: indices `[0..24]` are 25
`user_errtrig` NOTIFIC causes; indices `[25..49]` are the same 25 causes again as `amzn_errtrig`.
The 25-cause pattern, verbatim from the schema (index order):

| idx (user / amzn) | cause (verbatim description tail) |
|-------------------|-----------------------------------|
| 0..7 / 25..32 | `wr_buffer[0..7] full` — 8 HW write-buffer-full causes |
| 8..15 / 33..40 | `wr_buffer[0..7] drop` — 8 HW write-buffer-drop causes |
| 16 / 41 | instruction NQ write failed — **full SW notification queue** (does NOT fire if `SW_backpressure`=0 and `ignore_full`=1; `nq_full` shows which) |
| 17 / 42 | notification sent to a **disabled** instruction NQ (dropped) |
| 18 / 43 | HW-buffer-full **STALL** (`hw_backpressure` enabled; which buffer → INTC) |
| 19 / 44 | HW-buffer-full **DROP** (`hw_backpressure` disabled; which buffer → INTC) |
| 20 / 45 | AXI master received a **write response error** |
| 21 / 46 | AXI master **stalled** on max outstanding writes |
| 22 / 47 | SW-queue **threshold** reached (one-shot until that queue's `head_ptr` is written) |
| 23 / 48 | **overlap** detected in ≥2 enabled SW NQ |
| 24 / 49 | coalescer hit to **multiple streams** → AXI behavior non-deterministic |

So the FIS exports **two parallel 25-cause NOTIFIC error vectors** — a USER instruction-notification
path and an AMZN/privileged one — one feeding the MSIX `TRIG`, one the no_msix `TRIG`.

> **GOTCHA — count hazard `[HIGH · OBSERVED]`.** A naive `rg -c 'fis_errtrig'` returns **100**
> because it matches both the `trigger:` and `description:` lines. The true entry count is **50**
> (`rg -c '^- trigger: fis_errtrig_intr\['` = 50); the index range is `[0..49]`, split 25 user + 25
> amzn. Use the index range, not a raw line count.

### 3.5 The notific source backing

The 50 causes are generated by `notific_1_queue.json` (`AddrWidth 12` → `0x1000`, APB; bundles
`notific` (41 regs) + `notific_nq` (arr=`NUM_SW_Q`, 6 regs); **47** reg-defs total; **962**
instances). The status registers that drive the causes:

| reg | off | acc | feeds cause(s) |
|-----|-----|-----|----------------|
| `nq_full` | `0xc4` | RO | per-SW-NQ full status → causes 16, 22 (and the user/amzn NQ-full set) |
| `sw_backpressure` (`on[NUM_SW_Q-1:0]`) | `0x08` | RW | gates cause 16 |
| `nq_sw_overflow` (`ignore_full_en[NUM_SW_Q-1:0]`) | `0x18` | RW | the `ignore_full` of cause 16 |
| `hw_backpressure_lo/hi` (`on[31:0]`) | `0x0c`/`0x10` | RW | selects cause 18 (stall) vs 19 (drop) |
| `wr_buf_enable_lo/hi` | `0x30`/`0x34` | RW | the `wr_buffer[0..7]` enable domain (causes 0..15) |
| `nq_threshold_en` / `notific_nq.threshold` / `nq_threshold_passed` | `0xc0` / `+0x14` / `0xc8` | RW/RW/RO | cause 22 |
| `nq_error_addr_lo/hi` | `0x40`/`0x44` | RO | the errored notification address |

The wiring is therefore: **NOTIFIC status bit → errtrig `int_cause` latch → (severity wire-OR for
no_msix / MSI-X for msix) → `peb_intc` apex or PCIe host.** The exact NOTIFIC-status-bit → ordinal
`fis_errtrig_intr[N]` map is implied by the description text rather than a numbered schema table —
the bit→index ordinal is `[MED · INFERRED]`; the registers and the 50 cause strings are
`[HIGH · OBSERVED]`.

---

## 4. `spad` — the scratchpad surfaces `[HIGH · OBSERVED]`

### 4.1 No `spad` CSR regfile exists

There is **no** `spad`/`scratchpad` register file in *any* `csrs/` tree (cayman, mariana,
mariana_plus, sunda, maverick) — `fd` returns zero. The address map's only spad-named region is the
TPB `STATE_BUF_SCRATCH_RAM`, which is unrelated. "spad" resolves to two distinct artifacts: a
firmware struct and a hardware SRAM.

### 4.2 (1) The NCFW firmware `spad_ctrl` CC-op scratchpad `[CITED · HIGH]`

The NCFW (Neuron Collective FirmWare) walks a **SPAD control table** — an array of 8-byte
`spad_ctrl_entry` records the host stages in HBM and DMA-loads onto each TOP_SP — to drive
collectives. Each entry's `cc_op` command word encodes the collective: `algo_type` nibble (the
ring/mesh/hierarchical union selector) + `algo_sub_type` + completion/reporter flags +
channel/semaphore routing; the per-slot ring-step state (`slot_idx`, `run_state`, `repeat_cnt`,
the `m2s`/`s2m` semaphore counters); and the device **tsync** identity (`seng_id`/`tpb_id`/`dev_id`).
This is the *command set* of the collective engine and is documented in full in
[`../../collectives/ncfw/spad-ccop-tsync.md`](../../collectives/ncfw/spad-ccop-tsync.md). It is
firmware-resident scratchpad memory, **not** a CSR register file.

### 4.3 (2) The Maverick H-die hardware scratchpad `[HIGH · OBSERVED — header]`

The Maverick `arch-headers` (`fab/h_die_interconnect_connectivity_prime.yaml` + `address_map/.../seng.h`)
define `hdie_spad_s0..s7` — **8 die-to-die scratchpad SLAVE fabric ports per SEngine**:

- 8 ports `s0..s7` (fabric `id` 12..19), `dataw = 2048` bits each;
- each `MAVERICK_USER_INT_SENG_n_H_DIE_SCRATCHPAD_m` is `SIZE = 0x400000` (**4 MiB**), 8 contiguous
  banks (`SENG_0_H_DIE_SCRATCHPAD_0 @0xc080000000`, `_1 @0xc080400000`, `_2 @0xc080800000`,
  `_3 @0xc080c00000`, …);
- per SEngine — a per-engine bank of 8 × 4 MiB die-to-die scratchpads.

This is a raw addressable **SRAM** region (no bundles, no bitfields), the cross-H-die staging buffer
collective DMA moves through. The firmware `spad_ctrl` (§4.2) orchestrates collectives that may stage
in such scratchpad memory — the two "spad"s are the firmware-control and hardware-buffer halves of
the same concept (`[MED · INFERRED]` link).

> **NOTE — Maverick-only, and v5-interior is inferred `[HIGH · OBSERVED header / MED · INFERRED
> interior]`.** The H-die scratchpad is **Maverick (v5)-specific** in this checkout — the H-die
> interconnect is a multi-die concern; Cayman has **no** `hdie_spad` port (`rg -li hdie_spad` over the
> Cayman tree = 0). The geometry above is read directly from the Maverick header (OBSERVED); any
> dynamic/runtime behaviour of the v5 interior beyond the declared base/size/dataw is **INFERRED**.

---

## 5. Cross-generation divergence `[HIGH · OBSERVED]`

`fis_control` is the surface that moves across generations; the errtrig PAIR and notific are frozen.

| gen | bundles | reg-defs | field-defs | delta vs Cayman |
|-----|--------:|---------:|-----------:|-----------------|
| **cayman** | 7 | 36 | 49 | baseline |
| mariana | 8 | 40 | 63 | **+`fis_cntrl_intr` bundle** (4 regs @base `0x100`) |
| mariana_plus | 8 | 40 | 63 | byte-identical to mariana |
| sunda | — | — | — | no `fis_control` ships in this checkout |

The 7 shared bundles are byte-identical Cayman == Mariana (same decimal bases `0/24/40/48/76/120/140`),
and the Cayman go-header ends at `FisControlApbTimeoutCtrl` with no `FisCntrlIntr` struct —
confirming the 7-bundle Cayman shape. The Mariana-added `fis_cntrl_intr` bundle:

| reg | off | acc | content |
|-----|-----|-----|---------|
| `mask` | `0x0` | RW | 5 posted-wr SLVERR cause masks `[0..4]` |
| `clr_on_read` | `0x4` | RW | `status_reg` [0] (rst 1) · `apb_blk_error_addr` [1] (rst 1) — clear-on-read |
| `status` | `0x8` | RO | 5 posted-wr SLVERR cause status `[0..4]` |
| `apb_blk_error_addr` | `0xC` | RO | `val` [31:0] — the errored address |

The 5 causes are the **AMZN/USER chain × AMZN/USER/USER-FIS EP** posted-write APB SLVERRs. In Cayman
they are HW triggers only (`fis_cntrl_intr[0..4]`, §2.8); Mariana hardens them into a readable,
maskable, address-capturing latch — the same hardening pattern as `qos_prot`'s Cayman→Mariana
AXI-parity addition. The `apb_timeout` (§2.8) is the watchdog that produces these SLVERRs.

> **QUIRK — schema typo preserved `[HIGH · OBSERVED]`.** The Mariana `fis_cntrl_intr` field
> descriptions ship *"A posted write **restulted** AMZN chain … APB SLVERR"* across all five causes —
> quoted as-is.

The errtrig `intc_4grp` PAIR schema and `notific_1_queue` are byte-structurally identical across
Cayman / Mariana / Mariana+ / Sunda / Maverick; the 50-cause `fis_errtrig` vector is consistent
across leaves (25 user + 25 amzn). The HW H-die scratchpad is Maverick-only; the firmware `spad_ctrl`
struct spans gens with the same `cc_op`(1b)/`algo_type`(4b) field widths.

---

## 6. The FIS's three interrupt vectors, reconciled `[HIGH · OBSERVED]`

The FIS contributes exactly three interrupt source vectors, one from each of its three control
sub-regions (CTL / SPROT / ERRTRIG); all are `needs_cdc:false`, `edge_triggered:true`:

| vector | count | source region | causes |
|--------|------:|---------------|--------|
| `fis_cntrl_intr[0..4]` | **5** | CTL (`apb_timeout`/APB-chain) | EP posted-wr SLVERR; HW-only in Cayman, registered in Mariana (§5) |
| `fis_sprot_intr[0..5]` | **6** per sprot | SPROT | remapper-deny [0], R-delta-monitor [1], tmu-timeout [2], B-delta-monitor [3], qos_pmu-OR [4], spare [5] (see [remapper.md](remapper.md) / [qos-prot.md](qos-prot.md)) |
| `fis_errtrig_intr[0..49]` | **50** | ERRTRIG (notific) | 25 user + 25 amzn NOTIFIC causes (§3.4) |

These three vectors are the FIS's complete interrupt contribution; their routing into the
`peb_intc` apex (no_msix severity-OR upward) or the PCIe host (msix MSI-X) is the subject of the
primary cross-link, [`../interrupt/errtrig-fis-routing.md`](../interrupt/errtrig-fis-routing.md).
The per-master physical instance counts are enumerated in
[`../interrupt/physical-intc-instances.md`](../interrupt/physical-intc-instances.md).

---

## Provenance ledger

| claim class | tag | basis |
|-------------|-----|-------|
| every `fis_control` / `papb_bcast` scalar, bundle, field, bit, reset | `[HIGH · OBSERVED]` | `csrs/fis/{fis_control,papb_bcast}.json` (jq) |
| decimal-offset proof; `papb_bcast` `mask` `0xfffe` + grp0-unicast model | `[HIGH · OBSERVED]` | offset contiguity; reg description string |
| FIS container layout; 582 AMZN / 464 USER / 856 msix / 1068 no_msix / 962 notific / 962 pairs | `[HIGH · OBSERVED]` | flat address map (`rg -c`, `json`-binding count) |
| errtrig = 2×`intc_4grp` + notific generator; 50-cause vector (25+25) | `[HIGH · OBSERVED]` | `errtrig_*.yaml` + `*_triggers.yaml` |
| H-die scratchpad geometry (8 × 4 MiB, 2048-bit, ids 12..19, Maverick-only) | `[HIGH · OBSERVED]` | Maverick `arch-headers` (header-level) |
| `intc_4grp` per-group latch/route semantics (W0C cause, 4 severity ORs) | `[CITED · HIGH]` | [pkl-intc-sprot-security.md](../address/pkl-intc-sprot-security.md), not re-derived |
| firmware `spad_ctrl` CC-op struct (cc_op/algo_type/slot/tsync) | `[CITED · HIGH]` | [spad-ccop-tsync.md](../../collectives/ncfw/spad-ccop-tsync.md) |
| perf_snapshot → qos mirror stitch; notific-bit → cause ordinal; v5 interior | `[MED · INFERRED]` | cross-file semantics |
| **#908 Cayman "642 pairs"** vs byte-grounded **962 pairs** | `[HIGH · OBSERVED]` | CORRECTION raised §3.2 |
