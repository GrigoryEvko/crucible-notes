# CSR — qos_prot (FIS QoS / AXI NTS)

`qos_prot` is the **per-master AXI egress traffic-shaper + slave-protection responder** that
sits in every **Fabric Interface Slice (FIS)** on the privileged on-die APB-IO fabric. It is
a `0x1000` (4 KiB) `REGFILE` instanced **once per FIS**, where a FIS is the slice between one
fabric master (an SDMA channel, a D2D/PCIe/SDMA-H2D/D2H/TOP_SP block, …) and the AXI fabric.
Functionally it is two halves welded into one register file:

1. **the QoS shaper** (`csr` bundle) — three layered throttles on the five AXI5 channels
   (AR, R, AW, W, B) plus a fifteen-LFSR probabilistic staller;
2. **the "prot" (slave-protection) responder** (`nts_amzn` / `wr_serializer` /
   `nts_isolation`) — the **NTS (No-Target-Slave)** error generator that terminates an AXI
   transaction locally when its downstream slave is absent, plus an AXI write-ordering
   protocol checker and an isolation/quiescence control.

The byte-exact register layout, the shaping/arbitration model, the `prio_cap`-equivalent
priority mechanism, the NTS responder behaviour, and the `.mako` generator that emits the
file are all reconstructed below directly from the shipped Cayman register schema
(`csrs/sprot/qos_prot.json`) and its generator template (`qos_prot.json.mako`). The
page default is `[HIGH · OBSERVED]`; claims that depart from it carry an explicit
tag, and the per-claim-class breakdown is the Confidence ledger at the end.

> **Scope / naming clarification.** `qos_prot` does **not** generate
> `AxQOS`/`AxPROT[2:0]` sideband, and does **not** do master-ID / VMID / region gating. A
> full-file scan for `axqos`/`axprot`/`vmid`/`secure`/`privileged`/`master-id`/`region` field
> names returns **zero** hits (the only matches are the `UnitName` `qos_prot` and the
> `wr_serializer.status.error` "Protocol error" string). The **"prot"** here is *slave
> protection* (NTS) + AXI *protocol checking* (`wr_serializer`), **not** privileged/secure
> AxPROT emission. The address/ID half — master-ID/VMID gating and AxPROT `0x2` stamping —
> lives in the sibling [`remapper.md`](./remapper.md) in the same FIS sprot region (see
> [`../address/pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md) §4–5). The
> shaper throttles by **stalling / capping**, not by emitting a QoS value.

The companion observe surfaces are [`qos-pmu-hostvisible.md`](./qos-pmu-hostvisible.md) (the
host-visible monitor view + the DEBUG-FIS PMU); the AXI integrity watchdog in the same region
is [`nsm.md`](./nsm.md); FIS-level control/error-trigger is [`fis-errtrig-spad.md`](./fis-errtrig-spad.md).

---

## 1. Regfile-level facts

Read from `qos_prot.json`:

| property | value | note |
|----------|-------|------|
| `UnitName` | `qos_prot` | |
| `Type` | `REGFILE` | |
| `DataWidth` | `32` | 32-bit APB data |
| `AddrWidth` | `12` | → `0x1000` (4 KiB) window |
| `SizeInBytes` | `"0x1000"` | **hex string** (see GOTCHA) |
| bundle arrays | **5** | `csr`, `nts_amzn`, `wr_serializer`, `nts_isolation`, `spare_amzn` |
| register definitions | **74** | csr 61 · nts_amzn 5 · wr_serializer 3 · nts_isolation 1 · spare_amzn 4 |
| register instances | **74** | every `ArraySize="1"` — **no replicated bundles** |
| bitfield definitions | **192** | csr 168 · nts_amzn 11 · wr_serializer 3 · nts_isolation 6 · spare_amzn 4 |
| register `AccessType` | RO 17 · RW 56 · WO 1 | |
| bitfield `AccessType` | RO 20 · RW 171 · WO 1 | |
| `SpecialAccess` | **15 × `PulseOnW`** + 3 × `None` | the 15 LFSR `*_seed` regs reload-on-write; rest absent |
| `0xb1` reset placeholder | **absent** (`grep -ci 0xb1` = 0) | |

> **GOTCHA — hex-string sizes, no mixed decimal here.** Every size in
> this file is an explicit `0x`-prefixed **hex string**: `SizeInBytes="0x1000"` and the five
> `BundleSizeInBytes` (`"0x300"`,`"0x80"`,`"0x80"`,`"0x04"`,`"0x10"`). This file follows the
> `tdma_model` convention, **not** the `udma_gen` decimal-`BundleSizeInBytes` convention — so
> the usual "AddressOffset is hex but BundleSizeInBytes is decimal" trap does **not** bite
> here; both are hex. `ArraySize` is the **string** `"1"`, so any multiply must `tonumber()`
> it first (else `jq` string-concatenates). `AddressOffset` is hex and **relative to its
> bundle base**.

**Worked bundle-base computation** (the one nuance a reimplementer must get right). The
register's absolute window offset = `bundle.AddressOffset + register.AddressOffset`. Example:
`nts_amzn.read_data` (the `0xDEADBEEF` poison register):

```
bundle  nts_amzn  AddressOffset = "0x0400"        (hex string)
reg     read_data AddressOffset = "0x0c"          (hex string, relative to bundle base)
abs window offset = 0x0400 + 0x0c = 0x40c
```

Since every `ArraySize="1"`, no stride multiply is needed; the absolute offset of any field
is simply `int(bundle_off,16) + int(reg_off,16)`, and the FIS-absolute address is that plus
the FIS `..._SPROT_QOS` base from the address map.

---

## 2. Bundle map

Five arrays, no overlap; `BundleSizeInBytes` are hex strings, spans computed:

| base | end | `BundleSizeInBytes` | #reg | bundle | role |
|-----:|----:|--------------------:|-----:|--------|------|
| `0x000` | `0x300` | `0x300` | 61 | `csr` | **shaper**: limits + rate-window + fairness + 15 LFSRs + 5 stallers |
| `0x400` | `0x480` | `0x080` | 5 | `nts_amzn` | **NTS** no-target-slave error responder |
| `0x500` | `0x580` | `0x080` | 3 | `wr_serializer` | write-serialize enable + AXI protocol-error latch/clear |
| `0x600` | `0x604` | `0x004` | 1 | `nts_isolation` | NTS counter-rst / timeout-en / slice-rst (**Cayman-new**) |
| `0x7F0` | `0x800` | `0x010` | 4 | `spare_amzn` | `zeros_0/1`, `ones_0/1` |

The highest `csr` register is `b_stall @ +0x2c4` (`< 0x300`); the highest `nts_amzn`
register is `write_response @ +0x10` (`< 0x80`). Gaps `0x300–0x400`, `0x480–0x500`,
`0x580–0x600`, `0x604–0x7F0`, and `0x800–0xFFF` are reserved window headroom — the file
declares a `0x1000` window but only the lower `0x800` is populated, with `spare_amzn` pinned
to the **top** of that `0x800` sub-region (an AMZN convention).

---

## 3. The `csr` bundle — the QoS / shaping mechanism

This is the shaper. It layers **three independent throttle mechanisms** over the five AXI
channels, gated globally by a master `chicken` bit, plus a fifteen-LFSR probabilistic
**staller** for latency injection. All offsets below are relative to bundle base `0x000`
(so abs = the same value, `ArraySize=1`).

### 3a. Master enable — `control @ +0x00` (RW)

| bit | field | rst | meaning |
|----:|-------|----:|---------|
| 0 | `chicken` | **`0x1`** | **Disable ALL transaction modification at reset** → block boots **transparent**; SW must clear it to arm any shaping/stall |
| 1 | `lfsr_ar_prob_enable` | `0x0` | enable AR prob LFSR |
| 2 | `lfsr_ar_delay_1_enable` | `0x0` | |
| 3 | `lfsr_ar_delay_2_enable` | `0x0` | |
| 4 | `lfsr_r_prob_enable` | `0x0` | |
| 5 | `lfsr_r_delay_1_enable` | `0x0` | |
| 6 | `lfsr_r_delay_2_enable` | `0x0` | |
| 7 | `lfsr_aw_prob_enable` | `0x0` | |
| 8 | `lfsr_aw_delay_1_enable` | `0x0` | |
| 9 | `lfsr_aw_delay_2_enable` | `0x0` | |
| 10 | `lfsr_w_prob_enable` | `0x0` | |
| 11 | `lfsr_w_delay_1_enable` | `0x0` | |
| 12 | `lfsr_w_delay_2_enable` | `0x0` | |
| 13 | `lfsr_b_prob_enable` | `0x0` | |
| 14 | `lfsr_b_delay_1_enable` | `0x0` | |
| 15 | `lfsr_b_delay_2_enable` | `0x0` | (3 LFSRs × 5 channels = 15 enable bits) |
| 16 | `outstanding_read_limit_enable` | `0x0` | enable §3b read cap |
| 17 | `outstanding_write_limit_enable` | `0x0` | enable §3b write cap |

> **NOTE — the `chicken` boot semantic.** `chicken` rst=`0x1` means the
> entire shaper is *bypassed* out of reset: no limit, no stall, no fairness gate fires until
> firmware writes `control[0]=0`. A faithful rebuild **must** reset this bit to 1 — the FIS
> is required to be transparent until explicitly armed, so a missing target on the boot path
> is never silently shaped.

### 3b. Throttle 1 — outstanding-transaction limiter

A hard cap on in-flight AXI reads/writes, with a hysteresis floor.

**`outstanding_transaction_limits @ +0x10` (RW)**

| bits | field | rst | meaning |
|-----:|-------|----:|---------|
| 6:0 | `read_limit` | `0x0` | max reads outstanding — **schema: `0 = 128`** (7-bit, `0`→max) |
| 12:8 | `write_limit` | `0x0` | max writes outstanding — **schema: `0 = 32`** (5-bit, `0`→max) |

The `0`→max mapping means the reset value is *effectively unlimited at the field's full
count* (128 reads / 32 writes), so nothing is capped until SW programs a non-zero limit and
sets the `control[16]/[17]` enables.

**`outstanding_transaction_modification_threshold @ +0x14` (RW)** — the hysteresis floor:
stalls only begin once at least `threshold` txns are in flight.

| bits | field | rst | meaning |
|-----:|-------|----:|---------|
| 6:0 | `read_threshold` | `0x0` | min reads outstanding before modification fires |
| 12:8 | `write_threshold` | `0x0` | min writes outstanding before modification fires |
| 16 | `enable_read_threshold_for_unconditional` | `0x0` | use min thr for **unconditional** read stalls |
| 17 | `enable_read_threshold_for_conditional` | `0x0` | use min thr for **conditional** read stalls |
| 18 | `enable_write_threshold_for_unconditional` | `0x0` | |
| 19 | `enable_write_threshold_for_conditional` | `0x0` | |

### 3c. Throttle 2 — the priority / fairness arbiter (the `prio_cap` mechanism) — `fairness_control @ +0x20` (RW)

This register **is** the arbitration-weight / priority-cap model of `qos_prot`. There is **no
explicit DWRR weight vector** in this block; instead the arbiter is a *per-(masked)-block-ID
windowed quota* — a round-robin gate that caps how much any one Block ID may issue inside a
`2^interval`-clock window. The "priority cap" is expressed as the **masked Block-ID match**:
the FIS compares the incoming transaction's Block ID (after AND-masking with `block_id_mask`)
against `block_id`, and limits read/write transactions for the matching class.

| bits | field | rst | meaning |
|-----:|-------|----:|---------|
| 8:0 | `block_id` | `0x0` | Block ID to use when overriding (9-bit on Cayman) |
| 17:9 | `block_id_mask` | **`0x7f`** | Block-ID mask (which ID bits participate in the match) |
| 23:18 | `interval` | **`0xa`** | **shift count**: window = `2^interval` clocks (rst `0xa` → `2^10` = 1024 clks) |
| 24 | `enable_read_fairness` | `0x0` | enable the read fairness gate |
| 25 | `enable_write_fairness` | `0x0` | enable the write fairness gate |
| 26 | `override_block_id` | `0x0` | override the Block ID with bits `[8:0]` of this register |

> **The `prio_cap` model, for a reimplementer `[HIGH · OBSERVED fields · MED · classification]`.**
> The priority cap is **mask-and-match, windowed**: `(incoming_block_id & block_id_mask)`
> matched against `block_id`, gated over a `2^interval` window. `block_id_mask` is the
> "priority class" selector (the masked-off bits are *don't-care*); `interval` sets the
> averaging window; `override_block_id` lets firmware force a class onto every transaction
> regardless of the bus-supplied ID. This is the closest thing to a DWRR/priority arbiter in
> the block — a **per-class windowed quota**, *not* a weighted-round-robin weight table. The
> reset `block_id_mask=0x7f` + `interval=0xa` decode to "match the low 7 ID bits over a
> 1024-clock window", a sane transparent default that does nothing until
> `enable_read_fairness`/`enable_write_fairness` are set. The header `fairness_control`
> default `0x0028FE00` decodes to exactly this field set (`mask=0x7f`, `interval=0xa`).

### 3d. Throttle 3 — windowed rate / bandwidth limiter

A per-window leaky/token-bucket: at most `cnt` reads/writes **and** at most a 36-bit byte
budget may pass per `clocks_in_interval` window. This is the **bandwidth shaper**.

**`utilization_control @ +0x24` (RW)**

| bits | field | rst | meaning |
|-----:|-------|----:|---------|
| 26:0 | `clocks_in_interval` | **`0x400`** | #clocks in the window (1024 default) |
| 28 | `enable_read_limit` | `0x0` | enable #read-txn-per-window cap |
| 29 | `enable_write_limit` | `0x0` | enable #write-txn-per-window cap |
| 30 | `enable_read_byte_limit` | `0x0` | enable read-byte-budget cap (see QUIRK) |
| 31 | `enable_write_byte_limit` | `0x0` | enable write-byte-budget cap |

The per-window budgets (all `RW`):

| register | +off | field | rst | meaning |
|----------|-----:|-------|----:|---------|
| `read_limit_in_window` | `+0x28` | `cnt[30:0]` | `0x4000` | max #reads / window |
| `write_limit_in_window` | `+0x2c` | `cnt[30:0]` | `0x4000` | max #writes / window |
| `read_byte_limit_in_window_lo` | `+0x30` | `cnt[31:0]` | `0x4000` | read-byte budget bits `[31:0]` |
| `read_byte_limit_in_window_hi` | `+0x34` | `cnt[3:0]` | `0x0` | read-byte budget bits `[35:32]` |
| `write_byte_limit_in_window_lo` | `+0x38` | `cnt[31:0]` | `0x4000` | write-byte budget bits `[31:0]` |
| `write_byte_limit_in_window_hi` | `+0x3c` | `cnt[3:0]` | `0x0` | write-byte budget bits `[35:32]` |

The byte budgets are **36-bit** (`lo[31:0]` + `hi[3:0]`), i.e. up to 64 GiB per window. The
classification of the windowed limiter as "token/leaky bucket" is `[MED · INFERRED]`; the
field set, widths and resets are `[HIGH · OBSERVED]`.

> **QUIRK — `utilization_control[30]` description copy-paste `[MED · OBSERVED]`.** The
> schema `Description` for **both** bit 30 (`enable_read_byte_limit`) and bit 31
> (`enable_write_byte_limit`) literally reads *"Enable limiting bytes written"*. By **field
> name**, bit 30 is the **read**-byte enable (it gates `read_byte_limit_in_window_{lo,hi}`);
> the description is a schema copy-paste artifact, not a wiring error. Trust the field name.

### 3e. The 15 LFSRs — the staller PRNG bank

Layout: **5 channels `{ar, r, aw, w, b}` × 3 LFSRs `{prob, delay_1, delay_2}` = 15 LFSRs**,
each LFSR = **3 registers** at a `+0x4` stride from base `0x200`:

```
*_seed     [31:0] val RW  rst=0xFFFFFFFF  SpecialAccess=PulseOnW   (a write RELOADS the LFSR)
*_poly     [31:0] val RW  rst=0xFFFFFFFF  (feedback polynomial)
*_current  [31:0] val RO  rst=0x0         (live LFSR state, read-only)
```

The full 45-register address table (stride `0x4`, base `0x200`):

| channel | prob `{seed,poly,current}` | delay_1 `{seed,poly,current}` | delay_2 `{seed,poly,current}` |
|---------|---------------------------|-------------------------------|-------------------------------|
| AR | `0x200/0x204/0x208` | `0x20c/0x210/0x214` | `0x218/0x21c/0x220` |
| R | `0x224/0x228/0x22c` | `0x230/0x234/0x238` | `0x23c/0x240/0x244` |
| AW | `0x248/0x24c/0x250` | `0x254/0x258/0x25c` | `0x260/0x264/0x268` |
| W | `0x26c/0x270/0x274` | `0x278/0x27c/0x280` | `0x284/0x288/0x28c` |
| B | `0x290/0x294/0x298` | `0x29c/0x2a0/0x2a4` | `0x2a8/0x2ac/0x2b0` |

The **`prob`** LFSR drives the stall-probability compare; **`delay_1`/`delay_2`** drive the
stall-duration generation (single-LFSR vs the dual-LFSR "FV mode", see §3f). Writing any
`*_seed` (`PulseOnW`) reloads that LFSR from the seed — the only `SpecialAccess` in the file
(15 of them, one per LFSR).

### 3f. The 5 per-channel stallers `[HIGH · OBSERVED fields · MED · role]`

One staller per AXI channel: `ar_stall @ +0x2b4`, `r_stall @ +0x2b8`, `aw_stall @ +0x2bc`,
`w_stall @ +0x2c0`, `b_stall @ +0x2c4`. **All five are bit-identical in layout** (the `.mako`
emits them from one loop). Each is a latency-injection / fault-modelling staller: a channel
can be stalled deterministically (`staller_delay`) or probabilistically (LFSR vs
`stall_probability`), conditionally (on a chosen handshake) or unconditionally. All fields
`RW`, rst `0x0`:

| bits | field | meaning |
|-----:|-------|---------|
| 8:0 | `staller_delay` | #cycles to stall the channel |
| 9 | `staller_random_delay` | `0`=stall `staller_delay` cycles; `1`=random `1..staller_delay` |
| 10 | `stall_unconditionally` | `1`=stall regardless of trigger conditions |
| 11 | `stall_unconditionally_always` | `0`=by probability; `1`=stall 100% of the time |
| 12 | `stall_conditionally` | `1`=stall when trigger conditions met |
| 13 | `stall_conditionally_always` | `0`=by probability; `1`=stall 100% of the time |
| 14 | `fv_mode_probability` | `0`=`stall_probability` is a **limit**; `1`=it is a **bitmask** |
| 15 | `fv_mode_delay` | `0`=single-LFSR delay; `1`=FV dual-LFSR delay |
| 23:16 | `stall_probability` | 8-bit compare vs the 8 LSBs of the **prob** LFSR (mask or limit) |
| 24 | `trigger_on_rresp` | trigger on `RRESP & RVALID` |
| 25 | `trigger_on_bresp` | trigger on `BRESP & BVALID` |
| 26 | `trigger_on_wlast` | trigger on `WLAST & WVALID` |
| 27 | `trigger_on_araddr` | trigger on `ARADDR & ARVALID & ARREADY` |
| 28 | `trigger_on_awaddr` | trigger on `AWADDR & AWVALID & AWREADY` |
| 29 | `retrigger` | re-trigger when any trigger happens |
| 30 | `retrigger_max` | `0`=add new delay to remaining; `1`=delay = `max(new, remaining)` |

`csr` bundle field total = **168** (18 `control` + 2 `outstanding_limits` + 6 `mod_threshold`
+ 6 `fairness` + 5 `utilization` + 6 `*_in_window`-cnt + 45 LFSR + 5×16 staller).

---

## 4. The "prot" half — NTS, write-serializer, isolation, spares

### 4a. `nts_amzn` — the No-Target-Slave error responder (the protection core)

When the downstream slave is absent (powered off / unmapped / flushed), **NTS terminates the
AXI transaction locally** with a programmable response and a programmable read-data pattern,
so the fabric never hangs against a missing target. This is the literal "protection" in
`qos_PROT`.

**`control @ +0x00` (RW)**

| bit | field | rst | meaning |
|----:|-------|----:|---------|
| 0 | `bypass` | `0x0` | chicken bit to bypass the entire NTS |
| 1 | `enable` | `0x0` | SW enable of NTS mode (**OR'd** with the HW input-port value) |
| 2 | `mode` | `0x0` | state after flush: `0`=NO-TARGET, `1`=BLOCK |
| 3 | `chicken_nts_core_wr_fsm_fix` | **`0x1`** | remove `aw_pending` from the NTS Core Write FSM |

**`status @ +0x04` (RO)**

| bit | field | rst | meaning |
|----:|-------|----:|---------|
| 0 | `no_target_mode` | `0x0` | NTS mode requested by HW or SW |
| 1 | `flushing` | `0x0` | flushing outstanding transactions |
| 2 | `flushed` | `0x0` | all outstanding txns flushed, nothing pending |
| 3 | `mode` | `0x0` | state after flush: `0`=NO-TARGET, `1`=BLOCK |

**The response programming** (the bytes a reimplementer must reproduce exactly):

| register | +off | field | rst | meaning |
|----------|-----:|-------|----:|---------|
| `read_response` | `+0x08` | `val[1:0]` | **`0x2`** | read resp in NTS mode: `00`=OK, `01`=unused, `10`=**SLVERR**, `11`=DECERR |
| `read_data` | `+0x0c` | `val[31:0]` | **`0xDEADBEEF`** | data returned in NTS mode, **replicated across the bus width** |
| `write_response` | `+0x10` | `val[1:0]` | **`0x2`** | write resp in NTS mode: `00`=OK, `01`=unused, `10`=**SLVERR**, `11`=DECERR |

> **The NTS responder, end to end.** On reset the responder is *armed to
> fail safely*: both `read_response` and `write_response` default to **`0x2` = SLVERR**, and
> `read_data` defaults to the **`0xDEADBEEF`** poison pattern (replicated across the bus, so a
> 256-bit beat reads `DEADBEEF DEADBEEF …`). NTS engages when `control.enable` (or the HW
> no-target input) is asserted: `status.no_target_mode` latches, the responder *flushes*
> outstanding txns (`status.flushing` → `status.flushed`), and thereafter terminates every
> read with `{read_response, read_data}` and every write with `write_response`. `control.mode`
> selects the post-flush behaviour — **NO-TARGET** (keep responding with the poison pattern)
> vs **BLOCK** (hold the channel). The `01` response code is reserved/unused; only `0`/`2`/`3`
> are legal. The `0xDEADBEEF` default matches the region-wide poison convention — the sibling
> `nsm` watchdog uses the same `0xDEADBEEF` RDATA poison + SLVERR on a protocol violation
> (see [`nsm.md`](./nsm.md) and [`../address/pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md) §4c).

> **NOTE — the remapper DENY path reuses NTS.** Per
> [`../address/pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md) §4d, a CAM
> **miss/DENY** in the FIS address/ID half ([`remapper.md`](./remapper.md)) is terminated by
> *this same* NTS responder: a denied transaction is steered into the NTS and answered with
> SLVERR + `0xDEADBEEF`. So `nts_amzn` is both the "missing-slave" path **and** the
> "access-denied" path — one local AXI terminator serving the whole FIS sprot region.

### 4b. `wr_serializer` — AXI write ordering + protocol check

Serializes writes and latches an AXI write-channel protocol violation (the WDATA-before-AW
ordering checker).

| register | +off | field | access | rst | meaning |
|----------|-----:|-------|--------|----:|---------|
| `control` | `+0x00` | `enable[0]` | RW | `0x0` | enable write serialization |
| `status` | `+0x04` | `error[0]` | RO | `0x0` | protocol error detected by the serializer |
| `clear` | `+0x08` | `clear_error[0]` | **WO** | `0x0` | clear the error latch by writing `1` (**the only WO register in the file**) |

### 4c. `nts_isolation` — counter-reset / timeout / slice-reset (Cayman-new)

Isolation/quiescence controls: drain the NTS pending counters, arm the read/write timeout
counters, and software-reset the AXI register-slices on either side of the NTS so a stuck
master can be isolated and the FIS re-initialised without a full block reset.

**`ctrl @ +0x00` (RW)**

| bit | field | rst | meaning |
|----:|-------|----:|---------|
| 0 | `rd_reset` | `0x0` | reset read pending + outstanding txn counts |
| 1 | `wr_reset` | `0x0` | reset write pending + outstanding txn counts |
| 2 | `rd_timeout_en` | **`0x1`** | enable the read timeout counter inside the NTS core |
| 3 | `wr_timeout_en` | `0x0` | enable the write timeout counter inside the NTS core |
| 4 | `slv_slice_reset` | `0x0` | SW reset for the slices **between fabric and NTS** |
| 5 | `mstr_slice_reset` | `0x0` | SW reset for the slices **between NTS and the block** |

Note `rd_timeout_en` boots **enabled** (`0x1`): read timeouts are armed at reset so a stuck
read into a missing slave is bounded even before firmware touches the block.

### 4d. `spare_amzn` — spares

| register | +off | rst |
|----------|-----:|----:|
| `zeros_0` | `+0x0` | `0x0` |
| `zeros_1` | `+0x4` | `0x0` |
| `ones_0` | `+0x8` | `0xFFFFFFFF` |
| `ones_1` | `+0xC` | `0xFFFFFFFF` |

---

## 5. The `.mako` generator — how the file is parametrically emitted

`qos_prot.json` is generated from `qos_prot.json.mako`. The shaper's repetitive structure —
the 15 LFSR enable bits, the 45 LFSR registers, the 5 identical stallers — is **not written
by hand**; it is emitted by two nested Mako loops over the channel and LFSR-type lists. This
is the key structural insight: the channel set `{AR, R, AW, W, B}` and the LFSR-type set
`{prob, delay_1, delay_2}` are the generator's free parameters, and the register file is
their Cartesian product.

**(1) The 15 LFSR enable bits** (in `control`), bit-numbered by a running counter:

```mako
<% bit = 1 %>\
% for channel in [ 'ar', 'r', 'aw', 'w', 'b' ]:
    % for type in [ 'prob', 'delay_1', 'delay_2' ]:
                            {
                                "Name": "lfsr_${channel}_${type}_enable",
                                "AccessType": "RW",
                                "Position": "${bit}",
                                "Description": "Enable LFSR",
                                "ResetValue": "0x0"
                            },
        <% bit += 1 %>\
    % endfor
% endfor
```

After the 15 enables, `bit` lands on 16/17 for `outstanding_{read,write}_limit_enable`
(emitted with the same running `${bit}`). So `control`'s bit layout is *computed*, not
literal — the LFSR fan-out determines where the outstanding-limit enables land.

**(2) The 45 LFSR registers**, address-stepped by a running `offset` from `0x200`:

```mako
<% offset = 0x200 %>\
% for channel in [ 'AR', 'R', 'AW', 'W', 'B' ]:
    % for type in [ 'prob', 'delay_1', 'delay_2' ]:
                    {   "Name": "lfsr_${channel.lower()}_${type}_seed",
                        "AddressOffset": "${'{:#02x}'.format(offset)}",
                        ... "ResetValue": "0xffffffff", "SpecialAccess": "PulseOnW" },
        <% offset += 0x4 %>\
                    {   "Name": "lfsr_${channel.lower()}_${type}_poly",
                        "AddressOffset": "${'{:#02x}'.format(offset)}", ... },
        <% offset += 0x4 %>\
                    {   "Name": "lfsr_${channel.lower()}_${type}_current",
                        "AddressOffset": "${'{:#02x}'.format(offset)}", "AccessType": "RO", ... },
        <% offset += 0x4 %>\
    % endfor
% endfor
```

Three `offset += 0x4` per `(channel, type)` is exactly the `{seed, poly, current}` triple at
`+0x4` stride — which is why the §3e table is a perfectly regular `0x200 + 12·n` grid. The
`'{:#02x}'.format(offset)` is what makes every LFSR `AddressOffset` a lowercase `0x…` string.

**(3) The 5 per-channel stallers**, same `offset` counter continuing from the LFSR block, with
a trailing-comma guard so the **B** staller (the last register in the bundle) emits no comma:

```mako
% for channel in [ 'AR', 'R', 'AW', 'W', 'B' ]:
                    {   "Name": "${channel.lower()}_stall",
                        "AddressOffset": "${'{:#02x}'.format(offset)}", ... 16 fields ... }\
        % if channel != 'B':
,
        % endif
        <% offset += 0x4 %>\
% endfor
```

So the entire shaper — 15 enables, 45 LFSR regs, 5 stallers — is *one* `offset`-threaded
emission over the channel × LFSR-type product; the byte-exact `0x200…0x2c4` layout is a
direct consequence of the loop order (LFSRs first, then stallers, all `+0x4` stride). The
`nts_amzn` / `wr_serializer` / `nts_isolation` / `spare_amzn` bundles are written literally
(no loop), since they have no per-channel replication.

> **NOTE — `mode` field is literal in the Mako, not looped.** The
> `nts_amzn.control.mode` bit (the NO-TARGET/BLOCK select) and the `nts_isolation` bundle are
> hand-written in both the `.mako` and the emitted `.json` — they are the **Cayman additions**
> over the Sunda baseline (§6), bolted onto an otherwise-frozen template.

---

## 6. Cross-generation divergence (Cayman authoritative)

Comparing the shipped `csrs/sprot/qos_prot.json` across the five silicon (`sunda`, `cayman`,
`mariana`, `mariana_plus`, `maverick`). The shaper IP is **frozen from Cayman onward**; what
changed is at the edges.

| gen | bundles | reg-defs | bitfield-defs | delta |
|-----|--------:|---------:|--------------:|-------|
| sunda | 4 | 73 | 184 | oldest — **no `nts_isolation`**, 7-bit block-ID, no NTS `mode` |
| **cayman** | **5** | **74** | **192** | **this page** — adds `nts_isolation`, NTS `mode`, 9-bit block-ID |
| mariana / mariana_plus / maverick | 10 | 105 | 251 | adds AXI parity + AXI protocol-checker bundles |

The exact Sunda→Cayman diffs, byte-verified from both shipped JSONs:

1. **`fairness_control` widened 7→9-bit Block ID.** Sunda: `block_id [6:0]` / `block_id_mask
   [13:7]` / `interval [19:14]` / enables `[20]/[21]/[22]`. Cayman: `block_id [8:0]` /
   `block_id_mask [17:9]` / `interval [23:18]` / enables `[24]/[25]/[26]`. Both reset to the
   same semantic (`mask=0x7f`, `interval=0xa`); Cayman doubled the Block-ID space.
2. **`nts_amzn.control` gained the `mode` select.** Sunda has only `{bypass[0], enable[1],
   chicken_nts_core_wr_fsm_fix[2]}` (no `mode`). Cayman **inserts `mode` at bit[2]** (the
   NO-TARGET/BLOCK select) and pushes `chicken_nts_core_wr_fsm_fix` to **bit[3]**.
3. **`nts_isolation` is Cayman-new.** Sunda has only `{csr, nts_amzn, wr_serializer,
   spare_amzn}` (4 bundles, no NTS counter-reset / timeout-enable / slice-reset).

From Cayman to Mariana/+/Maverick the shared `csr` shaper is **byte-identical** (0 diffs over
all shared registers); the three newest gens only **bolt on** five AXI-hardening bundles
(`axi_m_parity`, `axi_s_parity`, `axi_checks_glbl`, `axi_checks_rd`, `axi_checks_wr`) plus
four NTS timeout-override registers. The shaper is frozen; Cayman is the intermediate that
introduced NTS isolation + the wider block-ID + the NTS mode select.

> **v5/MAVERICK caveat `[v5-interior · INFERRED]`.** The Maverick (`maverick`) header structure
> is OBSERVED on disk (same 10-bundle, 105-reg, 251-field shape as Mariana), but the v5
> *interior* semantics (the added parity/protocol-check bundles' runtime behaviour) are
> header-observed only and inferred byte-for-byte equal to Mariana — flag any v5-interior
> claim as INFERRED. The **Cayman** file is byte-grounded and authoritative for this page.

---

## 7. Physical placement — which fabric masters get a `qos_prot` `[HIGH · OBSERVED names · MED · placement role]`

`qos_prot` is the `..._SPROT_QOS` leaf of every `FIS_n_SPROT` container. The container pairs
the **address/ID half first, the QoS half second**, in AXI series:

```
PEB_APB_IO_<n>_AMZN_..._FIS_0_SPROT          (size 0x2000, privileged)
  +0x0000  ..._SPROT_AMZN_REMAPPER  0x1000   amzn_remapper.json   (FIRST  — address/ID gating)
  +0x1000  ..._SPROT_QOS            0x1000   qos_prot.json        (SECOND — shaping + NTS)  <== THIS
```

So one FIS sprot region = **`amzn_remapper`** (address/master-ID/VMID gating + AxPROT `0x2`
stamping, `0x1000`) + **`qos_prot`** (shaping + NTS protection, `0x1000`). The two halves are
complementary: `qos_prot` does shaping + slave-protection; `remapper` does the address/ID
protection that `qos_prot` does **not**. Masters that get a `qos_prot` FIS (from the flat
address map): SDMA channels, IO_D2D_SUBSYS die-to-die links, IO_PCIE_A/U + PEB_PCIE_M,
IO_SDMA_H2D / IO_SDMA_D2H, IO_TOP_SP scratchpads, IO_INTC_RDM, PEB.

**Privilege split.** `qos_prot` is instanced only under the **privileged**
`PEB_APB_IO_{0,1}` (+ BCAST) clusters — the secure/internal management view, secure-only (per
[`../address/pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md) §4a:
`qos_prot` = 0 user / 784 secure, `0x1000`). The host-visible `SE_USER` FIS uses a different,
cut-down schema, `qos_host_visible.json` (`0x800`, both views), paired with `user_remapper`
instead of `amzn_remapper`. The `qos_*` triad is three **complementary** views of the same
FIS QoS IP, **not** subset/superset:

* **`qos_prot`** = privileged shaping control + NTS protection (this page);
* **`qos_host_visible`** = host read-only monitoring view (per-window reads/writes/bytes,
  totals, per-channel backpressured cycles) — [`qos-pmu-hostvisible.md`](./qos-pmu-hostvisible.md);
* **`qos_pmu`** = the DEBUG-FIS PMU (event-select counters + AXI transaction matchers) —
  [`qos-pmu-hostvisible.md`](./qos-pmu-hostvisible.md).

The `qos_prot.csr` (61 control regs) and `qos_host_visible.qos_user` (52 monitor regs) share
**only the bundle shape** — zero register names in common; the host view drops *all* shaper
control and exposes read-only observability instead.

> **Relation to the in-engine SDMA QoS `[MED · INFERRED bridge]`.** `qos_prot` is the
> **fabric-edge** analogue of the SDMA engine's own QoS/arbitration
> ([`../../dma/dge-builder-qos.md`](../../dma/dge-builder-qos.md)): the engine's
> descriptor-level rate-limiter/DWRR shapes the DMA *scheduling inside* the engine, whereas
> `qos_prot`'s windowed limiter + fairness shape the **raw AXI traffic after it leaves the
> engine**, at the slice into the fabric — one shaper per master edge, shared by whatever
> engine drives that edge. The engine *counts* in-flight AXI txns (RO observability); the FIS
> *caps* them (the §3b outstanding limiter). Different layers of the same AXI path. No
> `AxQOS`/`AxPROT` is emitted at either layer by `qos_prot`.

---

## 8. Reimplementation summary

For a Vision-Q7-compatible GPSIMD control plane, the `qos_prot` contract a rebuild must honour:

1. **Boot transparent.** `csr.control.chicken` resets to `0x1` (all shaping bypassed) and
   `nts_isolation.ctrl.rd_timeout_en` resets to `0x1` (read timeouts armed). Nothing is shaped
   until firmware clears `chicken` and sets the per-mechanism enables.
2. **Three layered throttles**, all opt-in: (a) outstanding-txn caps (`read_limit` 7-bit
   `0→128`, `write_limit` 5-bit `0→32`, hysteresis floor); (b) the **`fairness_control`
   priority-cap** — masked Block-ID match (`block_id & block_id_mask` vs `block_id`) over a
   `2^interval` window, the block's only arbitration model (a per-class windowed quota, **not**
   a DWRR weight table); (c) the windowed rate/bandwidth limiter (#txn caps + 36-bit byte
   budgets per `clocks_in_interval`).
3. **The 15-LFSR / 5-staller bank** is a latency-injection / FV-modelling path; `*_seed` is
   `PulseOnW` (write reloads), `*_current` is RO live state.
4. **NTS must fail safe.** `read_response`/`write_response` reset to **SLVERR (`0x2`)`,
   `read_data` to **`0xDEADBEEF`** replicated across the bus; the same responder serves both
   the missing-slave path and the remapper DENY path. `control.mode` selects NO-TARGET vs
   BLOCK post-flush.
5. **Cayman widths are authoritative**: 9-bit Block ID, NTS `mode` present, `nts_isolation`
   present, 5 bundles / 74 reg-defs / 192 bitfields. (Sunda is narrower; Mariana+ adds AXI
   parity/protocol-check bundles on top of the frozen shaper.)

---

### Confidence ledger

* **`[HIGH · OBSERVED]`** — every count (5 bundles, 74 reg-defs, 192 bitfields, csr 168; reg
  AccessType RO17/RW56/WO1; bitfield RO20/RW171/WO1; 15 `PulseOnW`); every bundle base/size/
  span + no-overlap; every per-field bit-range/reset/access/description; the `0xb1` absence;
  the all-hex `BundleSizeInBytes`/`SizeInBytes` convention; the bundle-base math; the LFSR
  `0x200 + 12·n` grid + the 5-staller `0x2b4…0x2c4` block; the NTS SLVERR/`0xDEADBEEF`
  defaults + `chicken=1`/`rd_timeout_en=1` boot; the `.mako` loop structure; the Sunda↔Cayman
  field-width / `mode` / `nts_isolation` divergences; the FIS container layout + privilege
  split + the `qos_*` triad disjointness.
* **`[MED · INFERRED]`** — "token/leaky-bucket" classification of the windowed limiter;
  "priority-cap = masked-Block-ID windowed quota, not DWRR weights"; "latency-injection /
  FV-modelling" role of the staller bank; the fabric-edge↔in-engine-QoS bridge to
  [`../../dma/dge-builder-qos.md`](../../dma/dge-builder-qos.md).
* **`[v5-interior · INFERRED]`** — Maverick header shape OBSERVED, v5 interior semantics
  inferred equal to Mariana; Cayman is the byte-grounded authority for this page.

**Schema artifacts flagged inline** (not errors): `utilization_control[30]` description says
"bytes written" but by name is the **read**-byte enable (copy-paste); NTS `read_response`/
`write_response` encode `01` as "unused" (only `0`/`2`/`3` legal).
