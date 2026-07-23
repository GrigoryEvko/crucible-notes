# CSR — qos_pmu + qos_host_visible (the two "observe" views of the FIS QoS block)

`qos_pmu` and `qos_host_visible` are the **two read-out surfaces** of the per-master
**Fabric Interface Slice (FIS)** QoS block. Where [`qos_prot`](./qos-prot.md) **SHAPES** the
five AXI5 channels (outstanding-txn caps, a windowed rate/byte-budget limiter, a masked-Block-ID
fairness gate, a 15-LFSR / 5-channel staller, and the NTS no-target-slave responder), these two
register files **OBSERVE** that machinery. They carry **no shaping control whatsoever** — they are
pure telemetry. Together the three form the **QoS register triad**:

| role | regfile | window | domain | character |
|------|---------|-------:|--------|-----------|
| **CONTROL** (shape) | [`qos_prot`](./qos-prot.md) | `0x1000` | privileged AMZN | RW-dominant shaper + NTS |
| **MONITOR** (fixed) | `qos_host_visible` | `0x800` | **USER FIS** (host) | RO-dominant hard-wired counters |
| **PROFILE** (programmable) | `qos_pmu` | `0x800` | **USER DEBUG-FIS** | RW-dominant event PMU, next to CoreSight ELA-500 |

The triad is **three disjoint register sets** — zero register names in common between any pair.
It is **not** a subset/superset relation. `qos_host_visible` is the guest's always-on, zero-config
window into *how the shaper is throttling it*; `qos_pmu` is the deep, set-up-required AXI-event
profiler in the debug/trace domain. Both live in the **USER** (host-visible) address domain;
`qos_prot` is the only one in the privileged control leaf.

Everything below is reconstructed byte-exact from the shipped Cayman register schema
(`csrs/sprot/qos_pmu.json`, `csrs/sprot/qos_host_visible.json`), their `.json.mako` generators,
the FIS-level control schema (`csrs/fis/fis_control.json`), the flat address map
(`output/address_map/address_map_flat.yaml`), and the intc trigger YAML (`intc/sdma_triggers.yaml`).
The page default is `[HIGH · OBSERVED]`; claims that depart from it carry an explicit tag,
and the per-claim-class breakdown is the Confidence ledger at the end.

The sibling shaper is [`qos-prot.md`](./qos-prot.md) (the observed control surface — primary link);
the FIS AXI-integrity watchdog is [`nsm.md`](./nsm.md); the profiling/trace-debug gating that
governs the DEBUG-FIS domain is
[`../security/profiling-trace-debug-gating.md`](../security/profiling-trace-debug-gating.md); the
FIS sprot interrupt routing is
[`../interrupt/nsm-flow-unified.md`](../interrupt/nsm-flow-unified.md) and the privilege split is
[`../address/pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md).

---

## 1. Regfile-level facts

Both read from the Cayman JSON:

| property | `qos_pmu` | `qos_host_visible` |
|----------|-----------|--------------------|
| `UnitName` | `qos_pmu` | `qos_host_visible` |
| `Type` | `REGFILE` | `REGFILE` |
| `DataWidth` | `32` | `32` |
| `AddrWidth` | `11` → `0x800` (2 KiB) | `11` → `0x800` (2 KiB) |
| `SizeInBytes` | `"0x0800"` (hex string) | `"0x0800"` (hex string) |
| `RegfileFlavor` / `InterfaceType` | `null` / `null` (APB by xref) | `null` / `null` (APB by xref) |
| bundle arrays | **1** (`csr`) | **3** (`qos_user`, `nts_user`, `spare_user`) |
| register definitions | **146** | **64** |
| register instances | **146** (all `ArraySize="1"`) | **64** (all `ArraySize="1"`) |
| bitfield definitions | **154** | **67** |
| register `AccessType` | RO **32** · RW **114** | RO **60** · RW **4** |
| bitfield `AccessType` | RO **33** · RW **121** | RO **63** · RW **4** |
| `SpecialAccess` | none (absent) | **8 × `"None"`** (explicit, on `nts_user`); rest absent |
| distinct `ResetValue` | **all `0x0`** (154 fields) | **65 × `0x0`** + **2 × `0xffffffff`** (the two `ones_*` spares) |
| `IpReg` marker | **0 regs** | **52 / 64 regs** (`"true"` on every `qos_user` reg) |
| `0xb1` reset placeholder | **absent** (`grep -ci 0xb1` = 0) | **absent** (`grep -ci 0xb1` = 0) |
| `BundleSizeInBytes` | `"0x400"` | `"0x300"` · `"0x80"` · `"0x10"` (all hex) |

Key scalar observations:

* **Both windows are `0x800` (2 KiB), `AddrWidth=11` — exactly half `qos_prot`'s `0x1000`.**
  Confirmed by `SizeInBytes` *and* by the address-map node size `0x800`.
* **`qos_pmu` is RW-dominant (114/146 regs RW) — it is a CONFIG surface.** The only 32 RO regs
  are the 8 × 4 snapshot read-outs. **`qos_host_visible` is RO-dominant (60/64 regs RO) — it is a
  READ-OUT surface**; the only 4 RW regs are the `spare_user` zeros/ones. This RW-vs-RO split is
  the structural signature of "programmable profiler" vs "fixed-function monitor".
* **Every `qos_pmu` field resets to `0` ⇒ all counters boot DISABLED** (`event_select` one-hot
  `= 0` = no event selected); SW must arm them. The PMU is transparent at reset, the same boot
  contract as `qos_prot`'s `chicken=1`.
* **The `IpReg:"true"` marker** appears on all 52 `qos_user` registers and on **none** of the
  `qos_pmu` registers — a second machine-readable tag that the host monitor is fixed-function IP
  hardware, while the PMU is a configurable block.

> **GOTCHA — hex-string sizes, the "mixed hex/decimal" trap does NOT bite the `qos_*` files
>.** Every size in both files is an explicit `0x`-prefixed **hex string**:
> `SizeInBytes="0x0800"`, and `BundleSizeInBytes` `"0x400"` / `"0x300"` / `"0x80"` / `"0x10"`. The
> `qos_*` family follows the all-hex convention (same as `qos_prot`), so the usual
> "`AddressOffset` is hex but `BundleSizeInBytes` is decimal" pitfall does **not** apply here.
> `jq`'s `tonumber` cannot parse the hex-string offsets, so all span math must be done with
> `int(x, 16)`. The **only** place a decimal-offset regfile appears in this story is the
> *window-config* file `fis_control.json` (§6) — flagged there so the all-hex claim is correctly
> scoped to the `qos_*` files alone.

**Worked bundle-base computation** (the one nuance a reimplementer must get right). A register's
absolute window offset = `bundle.AddressOffset + register.AddressOffset`, both hex strings, no
stride multiply since every `ArraySize="1"`. Example — `qos_host_visible.nts_user.total_bytes_written_hi`:

```
bundle  nts_user               AddressOffset = "0x0400"   (hex string)
size    nts_user               BundleSizeInBytes = "0x80" (hex string = 128 dec)
reg     total_bytes_written_hi AddressOffset = "0x44"     (hex string, relative to bundle base)
abs window offset = int("0x0400",16) + int("0x44",16) = 0x444   (= 1092 dec)
in-bundle bound:  0x44 < 0x80  ok        bundle span [0x400 .. 0x480) within regfile 0x800  ok
```

The FIS-absolute address is then that `0x444` plus the FIS `..._SPROT_QOS` base from the address
map (§10).

---

## 2. `qos_pmu` — bundle map + counter/matcher geometry

A **single** bundle `csr` @ `0x0000`, `BundleSizeInBytes="0x400"`, `ArraySize="1"`, **146 regs**.
The highest register offset is `axi_txn_matcher3_reg19 @ 0x33c` (`+4 = 0x340` `< 0x400`, fits). The
`csr` bundle uses only the lower `0x400` of the `0x800` window; the upper `0x400` is reserved
headroom.

Register-group layout (mako loop bounds verified — §7):

| region | base | stride | count | regs |
|--------|-----:|-------:|------:|-----:|
| interrupt status/clear | `0x000` | — | 2 | `qos_pmu_intr_sts`, `qos_pmu_intr_clr` |
| PMU counters `0..7` | `0x100` | `0x20` | 8 | 8 regs each = **64** |
| AXI txn matchers `0..3` | `0x200` | `0x50` (20 × `0x4`) | 4 | 20 regs each = **80** |

Reconcile: `2 + 8·8 + 4·20 = 146` register-defs; `2 + 8·9 + 4·20 = 154` bitfields (the counter's
`cmp` register carries **2** fields, so 9 fields/counter, not 8). Both match the `jq` counts.

> **NOTE — the IP provisions 16 counters; Cayman wires 8 `[HIGH counts · MED interpretation]`.**
> The `0x100..0x1FF` counter region holds exactly 8 counters at `0x20` each (the live counters span
> `0x100..0x1FF`; `0x100 + 8·0x20 = 0x200`, where the matchers begin). But the
> `qos_pmu_intr_sts` / `qos_pmu_intr_clr` registers are **16-bit** (`val[15:0]`, one bit per
> counter), and the intc trigger phrase is "OR of all **16** PMU counter interrupts" (§8). Both
> reflect the **IP maximum (16)**; the Cayman build instances only **8** (mako `range(8)`). The
> unused upper `0x400` of the window is consistent with room for the other 8 counters + 4 more
> matchers in a larger variant.

---

## 3. `qos_pmu` — full per-field register table

All offsets are relative to the `csr` bundle base `0x000` (so abs = the same value, `ArraySize=1`).

### 3a. Interrupt — status + clear

| register | +off | bits | field | access | rst | meaning |
|----------|-----:|-----:|-------|--------|----:|---------|
| `qos_pmu_intr_sts` | `+0x00` | 15:0 | `val` | **RO** | `0x0` | interrupt status, one bit per PMU counter (16 provisioned, 8 live). HW sets, SW reads. |
| `qos_pmu_intr_clr` | `+0x04` | 15:0 | `val` | **RW** | `0x0` | write-1-to-clear the corresponding `intr_sts` bit |

The register-level `AccessType` is `RW` on both, but the **bitfield** access differs — `_sts.val`
is **RO** (HW-driven) and `_clr.val` is **RW** (SW-driven). The OR-reduction of all 16 status bits
is exported to the FIS as `fis_sprot_intr[4]` (§8).

> **NOTE — W1C semantics from prose `[HIGH fields · MED classification]`.** The clear register's
> behaviour is given in the `.mako`/`.json` description: *"Writing to the corresponding bit clears
> the qos_pmu_intr_sts register."* The write-1-to-clear idiom is the natural reading of "writing to
> the corresponding bit"; the schema does not encode a `SpecialAccess` to make this machine-explicit,
> so the W1C classification is inferred from the prose, not a flag.

### 3b. Per-counter block (`I` in `0..7`), base `0x100 + I·0x20`

8 registers per counter (the `cmp` register has 2 fields; all others 1):

| register | +off | bits | field | access | rst | meaning |
|----------|-----:|-----:|-------|--------|----:|---------|
| `pmu_counterI_event_select` | `+0x00` | 31:0 | `val` | RW | `0x0` | **one-hot** event selection (up to 32 sources) |
| `pmu_counterI_threshold_lo` | `+0x04` | 31:0 | `val` | RW | `0x0` | threshold value bits `[31:0]` |
| `pmu_counterI_threshold_hi` | `+0x08` | 31:0 | `val` | RW | `0x0` | threshold value bits `[63:32]` (⇒ **64-bit threshold**) |
| `pmu_counterI_cmp` | `+0x0c` | 2:0 | `snap0` | RW | `0x0` | comparison op used in the Snapshot0 window (3-bit code) |
| | | 6:4 | `snap1` | RW | `0x0` | comparison op used in the Snapshot1 window (3-bit code) |
| `pmu_counterI_snap0_lo` | `+0x10` | 31:0 | `val` | **RO** | `0x0` | Snapshot0 read-out bits `[31:0]` |
| `pmu_counterI_snap0_hi` | `+0x14` | 15:0 | `val` | **RO** | `0x0` | Snapshot0 read-out bits `[47:32]` (⇒ **48-bit** counter) |
| `pmu_counterI_snap1_lo` | `+0x18` | 31:0 | `val` | **RO** | `0x0` | Snapshot1 read-out bits `[31:0]` |
| `pmu_counterI_snap1_hi` | `+0x1c` | 15:0 | `val` | **RO** | `0x0` | Snapshot1 read-out bits `[47:32]` (⇒ **48-bit**) |

Absolute `event_select` offsets (stride `0x20`, mako-verified): `0x100, 0x120, 0x140, 0x160,
0x180, 0x1a0, 0x1c0, 0x1e0`.

The counter model, for a reimplementer:

* **`event_select` is a 32-bit ONE-HOT** — each set bit selects one event source. The event-source
  *enum* (which bit = read txn / write txn / byte budget / stall cycles / latency / outstanding
  occupancy / matcher-hit / …) is **not shipped** in the JSON or mako. `[HIGH field · LOW which-bit semantics]`
* **64-bit programmable threshold** (`threshold_lo` + `threshold_hi`). The counter raises its
  `intr_sts` bit when the comparison op (`cmp`) against this threshold is satisfied inside a
  snapshot window. `[HIGH fields · MED role]`
* **`cmp` is a 3-bit comparison-OPERATOR select per snapshot window** (8 ops each). Two independent
  windows (`snap0`, `snap1`) can apply different operators to the same counter vs the same 64-bit
  threshold. The operator *enum* (e.g. `>`, `>=`, `==`, `<`) is **not shipped** (opaque 3-bit code).
  `[HIGH fields · LOW op enum]`
* **Each counter is (at least) 48-bit, double-buffered into snap0 / snap1** — `snap0` is captured
  at the window-0 boundary, `snap1` at the window-1 boundary, so SW can read a *frozen* value of one
  window while the other keeps counting (the classic atomic-read-of-a-moving-counter pattern). The
  freeze pulse comes from outside this file (§6). `[HIGH fields · MED "double-buffer/freeze" classification]`

> **QUIRK — `pmu_counterI_cmp` description copy-paste.** The `Description` of
> **all eight** `cmp` registers literally reads *"Comparison to be used for PMU Counter 0"* — the
> `${i}` substitution is missing from this one line of the generator (the template hardcodes
> `Counter 0` while every *register name* correctly carries `${i}`). It is a schema-text artifact,
> not a wiring error: trust the register name (`pmu_counter5_cmp` *is* counter 5's compare),
> ignore the "0" in its description string. (Compare the neighbouring `threshold_lo/hi` and
> `event_select` descriptions, which *do* interpolate `${i}` correctly.)

### 3c. AXI transaction matchers (`M` in `0..3`), base `0x200 + M·0x50`

| register | +off | bits | field | access | rst | meaning |
|----------|-----:|-----:|-------|--------|----:|---------|
| `axi_txn_matcherM_reg0` .. `axi_txn_matcherM_reg19` | `+0x00` .. `+0x4c` | 31:0 | `val` | RW | `0x0` | opaque "Pattern" — 20 × 32 bit = **640 bits** per matcher |

Matcher base offsets (stride `0x50`, mako-verified): `0x200, 0x250, 0x2a0, 0x2f0`.

Four matchers, each **20 × 32-bit = 640 bits** of "Pattern". A matcher is an AXI-beat classifier:
640 bits is consistent with a `{match-value, mask}` CAM over a wide AXI sideband vector
(`ARID`/`AWID`, `AxADDR`, `AxLEN`/`SIZE`/`BURST`, `AxQOS`, `RRESP`/`BRESP`, user bits, …) — i.e.
"count transactions matching THIS pattern". The matcher output is what an `event_select` one-hot bit
can point a counter at, making the PMU programmable beyond the fixed event list. The internal
bit-layout of the 640-bit pattern is **not decoded** in the JSON/mako (all 80 regs are a generic
"Pattern" loop). `[HIGH register geometry · MED "match-value+mask CAM" · LOW exact field map]`

---

## 4. `qos_host_visible` — bundle map

Three arrays, no overlap; `BundleSizeInBytes` hex strings, spans computed:

| base | end | `BundleSizeInBytes` | #reg | bundle | role |
|-----:|----:|--------------------:|-----:|--------|------|
| `0x000` | `0x300` | `0x300` | **52** | `qos_user` | traffic monitor — per-channel / per-window / total counters + deltas |
| `0x400` | `0x480` | `0x080` | **8** | `nts_user` | live NTS occupancy + NTS-mode traffic totals |
| `0x7F0` | `0x800` | `0x010` | **4** | `spare_user` | `zeros_0/1`, `ones_0/1` |

The highest `qos_user` register is `write_channel_delta @ +0xd4` (`< 0x300`); the highest
`nts_user` is `total_bytes_written_hi @ +0x44` (`< 0x80`); `spare_user` is pinned to the **top** of
the window (`0x7F0`, last 16 B — an AMZN convention identical to `qos_prot.spare_amzn`). Gaps
`0xd8–0x3FF`, `0x480–0x7EF` are reserved headroom; the `0x800` window is fully spanned with the
spare at the top.

---

## 5. `qos_host_visible` — full per-field register table

All counters `RO`, `rst=0x0`, field `cnt` unless noted. The richer descriptions below merge the
JSON text with the `.json.mako` text (the JSON shortened some — e.g. JSON "Number of reads" vs mako
"…in the control window").

### 5a. `qos_user` — control-window totals (the lifetime / since-reset view)

| register | +off | bits | width | meaning |
|----------|-----:|-----:|------:|---------|
| `total_reads` | `+0x00` | 30:0 | 31-bit | total reads in the control window |
| `total_writes` | `+0x04` | 30:0 | 31-bit | total writes in the control window |
| `total_bytes_read_lo` | `+0x08` | 31:0 | — | bytes read `[31:0]` |
| `total_bytes_read_hi` | `+0x0c` | 15:0 | **48-bit** | bytes read `[47:32]` |
| `total_bytes_written_lo` | `+0x10` | 31:0 | — | bytes written `[31:0]` |
| `total_bytes_written_hi` | `+0x14` | 15:0 | **48-bit** | bytes written `[47:32]` |

> **CORRECTION vs `qos_prot` byte widths.** The byte counters here are **48-bit**
> (`lo[31:0]` + `hi[15:0]`), but `qos_prot`'s byte-**LIMIT** fields are only **36-bit**
> (`read_byte_limit_in_window_lo[31:0]` + `…_hi[3:0]`, [see `qos-prot.md §3d`](./qos-prot.md)). This
> is **not** a contradiction: the *monitor* counts up to `2^48` bytes per window, while the *limiter*
> caps at the narrower 36-bit budget (≤ 64 GiB/window). A rebuild must size these counters to 48 bits
> even though the shaper they observe carries 36-bit budgets — observe-wider-than-you-cap is intentional.

"The control window" is `qos_prot`'s `utilization_control.clocks_in_interval` window
([`qos-prot.md §3d`](./qos-prot.md)): these totals are the running count of what the shaper has
admitted. `[HIGH fields · MED control-window linkage]`

### 5b. `qos_user` — per-monitor-window (×2 windows) × (live + perf snapshot)

For each window `w` in `{0, 1}`, every metric exists as a **live** register and a **frozen** `perf_`
mirror. Field `cnt`, all `RO`, all `rst=0x0`:

| metric | live (window 0 / 1) | perf mirror (window 0 / 1) | width |
|--------|---------------------|----------------------------|------:|
| reads | `+0x20` / `+0x24` | `+0x28` / `+0x2c` | 30:0 |
| writes | `+0x30` / `+0x34` | `+0x38` / `+0x3c` | 30:0 |
| bytes read `{lo,hi}` | `+0x40,+0x44` / `+0x48,+0x4c` | `+0x50,+0x54` / `+0x58,+0x5c` | 48-bit |
| bytes written `{lo,hi}` | `+0x60,+0x64` / `+0x68,+0x6c` | `+0x70,+0x74` / `+0x78,+0x7c` | 48-bit |

* `reads_in_monitor_window_w` — "reads in monitor window_w **since the last reset or
  perf_snapshot[w]**" (LIVE).
* `perf_reads_in_monitor_window_w` — "reads in monitor window_w, **snapshotted on the last
  perf_snapshot[w]**" (FROZEN).

The LIVE register keeps counting; the `perf_` mirror is atomically frozen at the last
`perf_snapshot[w]` event (programmed via `fis_control.perf_snapshot_{lo,hi}_w`, §6). This is the
snapshot/freeze pair: read `perf_` for a coherent window total, read the live reg for instantaneous
progress. Two windows let SW run e.g. a short and a long averaging window concurrently.

> **NOTE — `perf_snapshot[w]` is in the shipped JSON.** The phrases "since the
> last reset or perf_snapshot[w]" and "snapshotted on the last perf_snapshot[w]" appear **34×** in
> the shipped `qos_host_visible.json` itself, not only in the mako — so the *existence* of the
> per-window freeze event is OBSERVED. Only the *wiring* of `perf_snapshot[w]` **back** to
> `fis_control.perf_snapshot_{lo,hi}_w` is the cross-file INFERRED part (§6).

### 5c. `qos_user` — per-AXI-channel backpressure (the STALL view)

For each channel `ch` in `{ar, r, aw, w, b}`, four registers in the mako-emitted order
**(live w0, perf w0, live w1, perf w1)** at `0x4` stride, `0x10` per channel:

| channel | live w0 | perf w0 | live w1 | perf w1 |
|---------|--------:|--------:|--------:|--------:|
| `ar` | `0x80` | `0x84` | `0x88` | `0x8c` |
| `r` | `0x90` | `0x94` | `0x98` | `0x9c` |
| `aw` | `0xa0` | `0xa4` | `0xa8` | `0xac` |
| `w` | `0xb0` | `0xb4` | `0xb8` | `0xbc` |
| `b` | `0xc0` | `0xc4` | `0xc8` | `0xcc` |

Each register: `cnt[30:0]` RO `rst=0x0`, "cycles the `<ch>` was backpressured (valid and not ready)
in monitor window_w"; the `perf_` variant adds "snapshotted on the last perf_snapshot[w]".

> **CORRECTION — the channel layout is (live, perf) per window, NOT a uniform live/perf interleave
> `[HIGH · OBSERVED from mako]`.** The mako emits, **per channel**, an *inner* `for i in range(2)`
> over window that produces `${ch}_…_${i}` then `perf_${ch}_…_${i}`. So the true byte order is
> `ar_0 @0x80`, `perf_ar_0 @0x84`, `ar_1 @0x88`, `perf_ar_1 @0x8c` — i.e. **(live_w0, perf_w0,
> live_w1, perf_w1)**, not (live_w0, live_w1, perf_w0, perf_w1). A reimplementer reading these as
> "all live first, then all perf" would mis-map windows 0/1; the JSON offsets above are authoritative.

This is **stall-cycle accounting per AXI channel** — how many cycles each of the 5 channels asserted
`VALID` but saw `NREADY` (was backpressured). It is the direct OBSERVE side of `qos_prot`'s 5
per-channel stallers + 3 throttle mechanisms: when the shaper stalls a channel (LFSR staller,
outstanding cap, window limiter), these counters tick. `[HIGH fields · HIGH/INFERRED stall linkage]`

### 5d. `qos_user` — delta-outstanding (the delta-monitor that fires `intr[1]`/`intr[3]`)

| register | +off | bits | field | access | rst | meaning |
|----------|-----:|-----:|-------|--------|----:|---------|
| `read_channel_delta` | `+0xd0` | 8:0 | `ar_delta` | RO | `0x0` | AR delta count (signed 9-bit) |
| | | 17:9 | `r_delta` | RO | `0x0` | R delta count (signed 9-bit) |
| `write_channel_delta` | `+0xd4` | 8:0 | `aw_delta` | RO | `0x0` | AW delta count (signed 9-bit) |
| | | 17:9 | `w_delta` | RO | `0x0` | W delta count (signed 9-bit) |
| | | 26:18 | `b_delta` | RO | `0x0` | B delta count (signed 9-bit) |

The **delta-monitor**. Each field is a **signed 9-bit** running difference between a request channel
and its matching response channel (AR vs R, AW vs B; plus the W/AW request beats). Normal flow keeps
delta `>= 0` (requests precede/equal responses). A **negative** delta = more responses than requests
= a protocol anomaly / spurious-response / response underflow. The host *reads* the live signed
delta here; the *underflow event* raises a FIS sprot interrupt — **not** a `qos_pmu` interrupt:

* `fis_sprot_intr[1]` = "delta monitor interrupt for R responses more than AR request" (`read_channel_delta` underflow)
* `fis_sprot_intr[3]` = "delta monitor interrupt for B responses more than AW request" (`write_channel_delta` underflow)

`[HIGH fields + intr names · HIGH/OBSERVED intr[1]/[3] mapping]`

### 5e. `nts_user` — live NTS occupancy + NTS-mode totals

All `RO`, `rst=0x0`, field `count`, all carry `SpecialAccess:"None"` (the 8 explicit ones — §1):

| register | +off | bits | meaning |
|----------|-----:|-----:|---------|
| `outstanding_reads` | `+0x20` | 7:0 | live in-flight reads through this NTS (**8-bit**) |
| `outstanding_writes` | `+0x24` | 5:0 | live in-flight writes through this NTS (**6-bit**) |
| `total_reads` | `+0x30` | 31:0 | reads while in NTS mode since last reset |
| `total_writes` | `+0x34` | 31:0 | writes while in NTS mode since last reset |
| `total_bytes_read_lo` | `+0x38` | 31:0 | NTS-mode bytes read `[31:0]` |
| `total_bytes_read_hi` | `+0x3c` | 15:0 | NTS-mode bytes read `[47:32]` (48-bit) |
| `total_bytes_written_lo` | `+0x40` | 31:0 | NTS-mode bytes written `[31:0]` |
| `total_bytes_written_hi` | `+0x44` | 15:0 | NTS-mode bytes written `[47:32]` (48-bit) |

> **NOTE — occupancy width = cap width + 1.** `outstanding_reads` is **8-bit**
> and `outstanding_writes` is **6-bit**, exactly **one bit wider** than `qos_prot`'s
> `read_limit[6:0]` (7-bit, `0→128`) and `write_limit[12:8]` (5-bit, `0→32`)
> ([`qos-prot.md §3b`](./qos-prot.md)). The +1 bit of headroom lets the live occupancy counter
> represent the full cap value plus the boundary, so the monitor never wraps at the limit it is
> watching. This is the direct OBSERVE of `qos_prot`'s outstanding-transaction limiter.

The `total_*` registers measure how much traffic the `qos_prot.nts_amzn` no-target-slave responder
absorbed (the SLVERR / `0xDEADBEEF` path, [`qos-prot.md §4a`](./qos-prot.md)) while the slave was
absent — letting SW size the impact of a missing / powered-down target. `[HIGH fields · MED "sizes NTS impact" role]`

### 5f. `spare_user`

| register | +off | bits | field | access | rst |
|----------|-----:|-----:|-------|--------|----:|
| `zeros_0` | `+0x0` | 31:0 | `spares` | RW | `0x0` |
| `zeros_1` | `+0x4` | 31:0 | `spares` | RW | `0x0` |
| `ones_0` | `+0x8` | 31:0 | `spares` | RW | `0xffffffff` |
| `ones_1` | `+0xC` | 31:0 | `spares` | RW | `0xffffffff` |

Identical to the `qos_prot.spare_amzn` convention — these are the **only 4 RW registers** in the
file; everything else is RO observability.

---

## 6. Where the windows are set — the snapshot/threshold config

> **CRITICAL FINDING — the window/snapshot config is in NONE of the `qos_*` JSONs
>.** Neither `qos_host_visible` nor `qos_pmu` nor `qos_prot` contains a
> `perf_snapshot` trigger, a monitor-window-period register, or a freeze control. A full-tree scan
> for `perf_snapshot` / `monitor_window` / `snapshot_trigger` / `freeze` over `csrs/` resolves the
> trigger source to **`csrs/fis/fis_control.json`** — the FIS-level control regfile, `AddrWidth=13`
> → `0x2000`, placed on the **privileged** side. A reimplementer cannot find the snapshot model by
> reading the `qos_*` files alone.

`fis_control.sw_cntrl` bundle — **DECIMAL `AddressOffset`s** (a different convention from the all-hex
`qos_*` files):

| register | @off (dec) | bits | field | rst | meaning |
|----------|-----------:|-----:|-------|----:|---------|
| `fis_sprot_reset` | `12` | 31:0 | `rst` | `0` | SW reset of the sprot sub-block (resets the counters `qos_host_visible` reads) |
| `fis_nts_reset` | `16` | 31:0 | `rst` | `0` | SW reset of the NTS sub-block |
| `apb_user_decode` | `24` | 0 | `user1_en` | `0` | |
| | | 8 | `user2_en` | `0` | |
| | | 16 | `user_fis_en` | **`1`** | enables the **USER FIS** region (where `qos_host_visible` lives) |
| | | 24 | `user_debug_en` | **`1`** | enables the **USER DEBUG-FIS** region (where `qos_pmu` lives) |
| `perf_snapshot_lo_0` | `28` | 31:0 | `count` | `0` | window-0 snapshot period/value `[31:0]` |
| `perf_snapshot_hi_0` | `32` | 15:0 | `count` | `0` | window-0 snapshot period/value `[47:32]` (48-bit) |
| `perf_snapshot_lo_1` | `36` | 31:0 | `count` | `0` | window-1 snapshot period/value `[31:0]` |
| `perf_snapshot_hi_1` | `40` | 15:0 | `count` | `0` | window-1 snapshot period/value `[47:32]` (48-bit) |

> **NOTE — two independent region enables.** `user_fis_en` (bit 16) gates the
> host monitor's region; `user_debug_en` (bit 24) gates the debug PMU's region. Both reset to **`1`**
> — both regions are live out of reset — and they can be gated **separately**: the always-on host
> telemetry and the deep-debug PMU are two independently-enableable surfaces. The debug-domain gate
> is the same one discussed in
> [`../security/profiling-trace-debug-gating.md`](../security/profiling-trace-debug-gating.md).

**The snapshot/window model, stitched across files** `[HIGH per-register fact · MED cross-file stitch]`:

1. `fis_control.perf_snapshot_{lo,hi}_{0,1}` sets the two **48-bit** snapshot windows (period or
   compare value). When window `w` expires, `perf_snapshot[w]` fires.
2. `perf_snapshot[w]` atomically **copies** the live monitor counters into the `perf_` mirrors in
   `qos_host_visible` (§5b/5c) **and** advances the `qos_pmu` snapshot double-buffer (snap0/snap1, §3b).
3. `qos_pmu`'s per-counter `cmp.snap0`/`snap1` then applies a comparison op vs the 64-bit threshold
   within each window; a satisfied compare sets the counter's `intr_sts` bit; the OR raises
   `fis_sprot_intr[4]`.
4. `qos_prot`'s "control window" (`utilization_control.clocks_in_interval`) is a **separate**,
   shorter window the shaper uses for its rate/byte budget; the "total … in the control window"
   counters (§5a) observe **that** window.

The `perf_snapshot[w] → qos_host_visible`-mirror relation is OBSERVED (the phrase is in the shipped
JSON 34×); only the wiring that `perf_snapshot[w]` **is driven by** `fis_control.perf_snapshot_{lo,hi}_w`
is INFERRED — from the matching reg names and the two-window (0/1) correspondence, not from a single
explicit artifact. There is **no self-contained "freeze" bit** in any `qos_*` file; the freeze is the
external `perf_snapshot[w]` pulse from `fis_control`. `[HIGH facts · MED stitch]`

---

## 7. The `.mako` generators — parametric emission

### 7a. `qos_pmu.json.mako` — the per-counter / per-matcher loops

The two intr registers are literal; the 8 counters and 4 matchers are emitted by nested loops over
a running `offset`:

```mako
<% offset = 0x100 %>\
% for i in range(8):
    {   "Name": "pmu_counter${i}_event_select", "AddressOffset": "${'{:#02x}'.format(offset)}", ... }
    <% offset += 0x4 %>\
    {   "Name": "pmu_counter${i}_threshold_lo", ... }   <% offset += 0x4 %>\
    {   "Name": "pmu_counter${i}_threshold_hi", ... }   <% offset += 0x4 %>\
    {   "Name": "pmu_counter${i}_cmp",
        "Description": "Comparison to be used for PMU Counter 0",   ## <-- the ${i} miss (QUIRK §3b)
        "BitFields": [ {"Name":"snap0","Position":"2:0",...}, {"Name":"snap1","Position":"6:4",...} ] }
    <% offset += 0x4 %>\
    {   "Name": "pmu_counter${i}_snap0_lo", ... } ... snap0_hi ... snap1_lo ... snap1_hi
    <% offset += 0x4 %>\
% endfor

% for i in range(4):
    % for j in range(20):
    {   "Name": "axi_txn_matcher${i}_reg${j}", "Description": "Pattern bits",
        "AddressOffset": "${'{:#02x}'.format(offset)}", ... "Description":"Pattern" ... }\
        % if (i!=3 or j!=19):
,
        %endif
        <% offset += 0x4 %>\
    % endfor
% endfor
```

Eight `offset += 0x4` per counter ⇒ the `0x20` counter stride; `4 × 20` `offset += 0x4` matcher
registers ⇒ the `0x50` matcher stride. The trailing-comma guard `if (i!=3 or j!=19)` suppresses the
comma on the very last register (`axi_txn_matcher3_reg19`). The free parameters are exactly
`range(8)` (counters, IP-max 16) and `range(4)×range(20)` (matchers, 640-bit each).

### 7b. `qos_host_visible.json.mako` — the per-window / per-channel loops

The six control-window totals are literal; the per-window and per-channel blocks are loops over a
running `offset` from `0x20`:

```mako
<% offset = 0x20 %>\
% for i in range(2):  ## reads_in_monitor_window_${i}  ...
% for i in range(2):  ## perf_reads_in_monitor_window_${i}
% for i in range(2):  ## writes_... ;  next loop: perf_writes_...
## ... bytes_read (lo+hi) x2 ; perf_bytes_read x2 ; bytes_written x2 ; perf_bytes_written x2 ...

% for ch in [ 'ar', 'r', 'aw', 'w', 'b' ]:
    % for i in range(2):
        {   "Name": "${ch}_backpressured_cycles_${i}",      ... }   <% offset += 0x4 %>\
        {   "Name": "perf_${ch}_backpressured_cycles_${i}", ... }   <% offset += 0x4 %>\
    % endfor
% endfor
## read_channel_delta @0xd0 / write_channel_delta @0xd4   (literal, multi-field)
```

The **channel loop nests window inside channel** — for each channel it emits
`${ch}_…_${i}` then `perf_${ch}_…_${i}` for `i` in `{0,1}` — which is exactly why the byte order is
(live_w0, perf_w0, live_w1, perf_w1) per channel (the CORRECTION in §5c). `nts_user` and `spare_user`
are written literally (no loop).

---

## 8. Interrupt wiring — how `qos_pmu` reaches the intc `[HIGH · OBSERVED]`

The FIS exports a **6-entry sprot interrupt group**. From `intc/sdma_triggers.yaml` (Cayman; the
`trigger:` signal is `<master>_sprot_intr[N]`, the `name:` is `fis_sprot_intr_N`), all
`needs_cdc:false`, `edge_triggered:true`:

| idx | name | description (verbatim) | source |
|----:|------|------------------------|--------|
| 0 | `fis_sprot_intr_0` | amzn_remapper denied a transaction | remapper CAM deny ([`remapper`](./remapper.md)) |
| 1 | `fis_sprot_intr_1` | delta monitor interrupt for R responses more than AR request | `qos_host_visible.read_channel_delta` underflow (§5d) |
| 2 | `fis_sprot_intr_2` | tmu detected an AXI timeout | the FIS timeout unit (`qos_prot.nts_isolation.rd/wr_timeout_en`) |
| 3 | `fis_sprot_intr_3` | delta monitor interrupt for B responses more than AW request | `qos_host_visible.write_channel_delta` underflow (§5d) |
| 4 | `fis_sprot_intr_4` | qos pmu interrupt (OR of all 16 PMU counter interrrupts) | **`qos_pmu.qos_pmu_intr_sts` OR (§3a) — THIS PAGE** |
| 5 | `fis_sprot_intr_5` | fis_sprot_spare_0 | spare |

The "interrrupts" triple-`r` typo is verbatim from the YAML.

> **CORRECTION — `intr[5]` differs by generation.** In the **Cayman**
> `sdma_triggers.yaml`, `fis_sprot_intr_5 = "fis_sprot_spare_0"` (a spare). In the **maverick**
> trigger file the same index is repurposed as `"axi_checks interrupt"` — consistent with maverick's
> added `axi_checks_*` shaper bundles ([`qos-prot.md §6`](./qos-prot.md)). This page cites **Cayman
> as authoritative** (spare); the maverick repurpose is a downstream-generation change, not a Cayman
> fact. `[v5/MAVERICK header-OBSERVED · Cayman authoritative]`

So the `qos_pmu` interrupt and the two delta-monitor interrupts are **siblings in the same FIS sprot
group but come from different register files**:

* `intr[4]` qos_pmu ← `qos_pmu.qos_pmu_intr_sts` (OR of 16 counter bits) — **USER DEBUG-FIS**
* `intr[1]`/`intr[3]` delta ← the delta-monitor whose live state is
  `qos_host_visible.read/write_channel_delta` — **USER FIS**
* `intr[0]` deny ← the `user_remapper`/`amzn_remapper` CAM ([`remapper`](./remapper.md))
* `intr[2]` timeout ← the FIS `tmu` (cf. `qos_prot.nts_isolation.rd/wr_timeout_en`)

The FIS aggregates: remapper(deny) + delta-monitor(×2 underflow) + tmu(timeout) + qos_pmu(counter) +
1 spare. Different masters carry different sprot multiplicity (tpb: sprot 0..4; d2d/io_fabric/cc: 2;
sdma/top_sp/peb: 1) — one 6-entry group per FIS sprot the master owns. Each group feeds that master's
intc; routing is in [`../interrupt/nsm-flow-unified.md`](../interrupt/nsm-flow-unified.md).

---

## 9. Host-visible vs debug split, and what each observes `[HIGH · OBSERVED]`

`qos_host_visible` and `qos_pmu` are **not** a strict subset/superset — they are two orthogonal
views with zero register names in common. The "subset" framing is best read as *which facts each
domain can observe*:

| measurement | `qos_host_visible` (USER FIS) | `qos_pmu` (USER DEBUG-FIS) |
|-------------|-------------------------------|----------------------------|
| total reads/writes (control window) | YES (fixed) | indirectly (via event) |
| total bytes r/w (48-bit) | YES (fixed) | indirectly |
| per-window (×2) reads/writes/bytes | YES (fixed + perf mirror) | via snapshot windows |
| per-channel backpressure cycles | YES (5 ch × 2 win × 2) | indirectly (via event) |
| signed delta-outstanding (r/w chan) | YES (read-only state) | no (delta → intr[1]/[3], not pmu) |
| live NTS outstanding occupancy | YES (`nts_user`) | no |
| NTS-mode traffic totals | YES (`nts_user`) | no |
| programmable event counters | NO | YES (8 counters, IP-max 16) |
| AXI transaction matchers (640-bit CAM) | NO | YES (4 matchers) |
| 64-bit threshold + compare op | NO | YES (per counter, per window) |
| counter → interrupt | NO (delta → FIS intr) | YES (16-bit `intr_sts` → intr[4]) |

* **`qos_host_visible` = the FIXED-FUNCTION, ALWAYS-ON, GUEST-READABLE monitor.** It answers
  "how much traffic / how much was I throttled / am I leaking responses" with hard-wired counters
  the untrusted host can poll with no setup — pure RO observability (+4 spares), no config, no event
  programming, no interrupt-arming. It lives in the **USER FIS** leaf, paired with `user_remapper`.
* **`qos_pmu` = the PROGRAMMABLE, DEBUG-DOMAIN profiler.** It needs SW to program `event_select` +
  matchers + thresholds + `cmp` ops before it counts anything, and it can raise interrupts. It lives
  in the **USER DEBUG-FIS** leaf next to the CoreSight ELA-500 — the deep-debug / trace domain, not
  the always-on guest view.
* The two are **complementary, not nested**: `qos_host_visible` gives the guest a curated, fixed,
  zero-config telemetry set; `qos_pmu` gives debug SW a flexible, set-up-required, interrupt-capable
  superset of *event types* (anything a matcher can classify), but it does **not** re-expose the
  fixed `nts_user` occupancy / delta state. `[HIGH register sets · MED "curated vs flexible" framing]`

### 9a. Trust domain — what the untrusted host vs the secure side can reach `[HIGH · OBSERVED]`

* **`qos_prot` is secure-only.** Per [`../address/pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md),
  `qos_prot` is instanced **0 user / secure-only** under the privileged `PEB_APB_IO` clusters — the
  shaper controls are unreachable from the host.
* **Both `qos_*` "observe" files are in the USER (host-visible) domain.** `qos_host_visible`
  (1208 Cayman nodes) and `qos_pmu` (528 Cayman nodes) both live under `*_USER_FIS_*` paths (§10).
  So the **untrusted host can READ how the secure shaper is treating its traffic** (window counts,
  backpressure cycles, NTS occupancy, deltas) and can **program the debug PMU**, but it cannot change
  a single shaping parameter — every shaping control is in the secure-only `qos_prot`. This is the
  privilege contract of the triad: **observe in USER, shape in SECURE**.

### 9b. What each PMU observes of `qos_prot` `[HIGH facts · MED linkage]`

| `qos_prot` mechanism ([`qos-prot.md`](./qos-prot.md)) | observed by |
|--------------------------------------------------------|-------------|
| outstanding-txn limiter (read/write cap) | `nts_user.outstanding_reads/writes` (live occupancy) |
| windowed rate limiter (#txn/window) | `qos_user.reads/writes_in_monitor_window_{0,1}` |
| windowed byte budget (36-bit r/w bytes) | `qos_user.bytes_read/written_in_monitor_window` (48-bit) |
| control-window utilization | `qos_user.total_reads/writes/bytes` ("in the control window") |
| 5-channel staller + 3 throttles (stall) | `qos_user.{ar,r,aw,w,b}_backpressured_cycles` (stall cycles) |
| NTS no-target-slave responder | `nts_user.total_*` (traffic absorbed in NTS mode) |
| (AXI protocol health, not in `qos_prot`) | `qos_user.read/write_channel_delta` (response-vs-request balance) |
| (deep AXI event profiling) | `qos_pmu` 8 counters + 4 matchers (programmable) |

`qos_host_visible` is the **fixed mirror of the shaper**: every throttle in `qos_prot` has a
corresponding measurement (cap→occupancy, rate-window→window counts, byte-budget→byte counts,
staller→backpressure cycles, NTS→NTS-mode totals). `qos_pmu` is **shaper-independent**: it does not
mirror `qos_prot` 1:1; it counts arbitrary AXI events the 4 matchers classify, with thresholds and
interrupts, in the debug domain. `[HIGH register sets · MED mechanism map]`

---

## 10. Physical placement

Triad node counts from the **Cayman** `address_map_flat.yaml` (these are Cayman figures;
the `mariana` flat map gives different counts, see QUIRK):

| regfile | Cayman flat nodes | domain |
|---------|------------------:|--------|
| `qos_prot.json` | **1500** | privileged control |
| `qos_host_visible.json` | **1208** | USER FIS monitor |
| `qos_pmu.json` | **528** | USER DEBUG-FIS profiler |

Container layout (OBSERVED, Cayman flat map, `SDMA_0` example, exact bases):

```
USER FIS (host monitor) — qos_host_visible:
  APB_SE_0_USER_FIS_SDMA_0_FIS_0_SPROT            base 0x100C005000  size 0x1000
    +0x000  ..._SPROT_USER_REMAPPER  size 0x800   csrs/sprot/user_remapper.json
    +0x800  ..._SPROT_QOS            size 0x800   csrs/sprot/qos_host_visible.json   <== HOST MONITOR

USER DEBUG-FIS (debug profiler) — qos_pmu:
  APB_SE_0_USER_FIS_SDMA_0_DEBUG_FIS_0_SPROT      base 0x100C010000  size 0x4000
    +0x0000 ..._SPROT_ELA            size 0x1000  csrs/ela500/cxela500.json   (CoreSight ELA-500)
    +0x1000 ..._SPROT_QOS_PMU        size 0x800   csrs/sprot/qos_pmu.json     <== PMU
```

Key placement facts:

* **All 528 `qos_pmu` nodes live under `*_USER_FIS_*_DEBUG_FIS_0_SPROT_QOS_PMU`** (528/528 contain
  `DEBUG_FIS`); **all 1208 `qos_host_visible` nodes live under `*_USER_FIS_*_FIS_n_SPROT_QOS`**
  (1208/1208 non-debug). So `qos_host_visible` → the USER FIS sprot leaf (host monitor, paired with
  `user_remapper`); `qos_pmu` → the USER **DEBUG-FIS** sprot leaf (next to the ELA-500). Both are in
  the USER address domain; only `qos_prot` is in the privileged AMZN-side control leaf.
* **`qos_pmu`'s DEBUG-FIS-next-to-ELA placement confirms its role** as part of the on-die
  debug/trace subsystem (CoreSight), the deep profiling path — distinct from the always-on host
  telemetry of `qos_host_visible`. `[HIGH placement · HIGH/INFERRED role]`
* **Per-master breakdown** for `qos_pmu` (one `APB_IO_0_USER` cluster, from the flat map):
  `IO_D2D_SUBSYS ×8` (4 nodes each), `IO_INTC_RDM ×4`, `IO_TOP_SP_0..9 ×2`, `SE_*_PCIE_S* ×2`,
  `IO_PCIE_A/U`, `IO_SDMA_H2D/D2H`, `PEB`, `PEB_PCIE_M` — one `qos_pmu` per debug-instrumented fabric
  master, the same master set as `qos_prot`/`qos_host_visible`. `[HIGH names + counts]`

> **QUIRK — node counts are per-generation; re-ground to the Cayman map.** The
> `mariana` `address_map_flat.yaml` gives `qos_pmu=516` / `qos_host_visible=1320` / `qos_prot=1940`,
> **different** from the Cayman `528` / `1208` / `1500`. The instance multiplicity differs by silicon
> (more/fewer fabric masters), so any node-count claim **must** name its address-map source. This
> page's `528/1208/1500` are the **Cayman** flat-map figures (cross-checked against the Cayman
> `address_map_json_xref.yaml`, which agrees). Do not carry the mariana counts onto Cayman.

---

## 11. Cross-generation provenance `[MED]`

> **PROVENANCE LIMITATION.** Unlike `qos_prot` (which ships a 5-generation
> arch-headers corpus, [`qos-prot.md §6`](./qos-prot.md)), the byte-exact **field tables** in §3
> and §5 are derived from the **Cayman** `qos_pmu.json` / `qos_host_visible.json`. The other
> generations ship only thin schema copies under `arch-headers/{mariana,mariana_plus,maverick,sunda}/`,
> and a full 5-gen byte-diff of the PMU/monitor *interior* is not possible the way it was for
> `qos_prot`. The `qos_prot` cross-gen claims do **not** transfer field-for-field to these two files.

INFERRED cross-gen expectations, from the `qos_prot` evolution pattern `[MED/LOW · INFERRED]`:

* **Sunda** (oldest, 7-bit block-ID, no `nts_isolation`): a monitor of similar shape likely exists
  but **may lack** the `nts_user` occupancy regs (Sunda had no NTS isolation/quiescence) and **may
  have narrower delta fields**. Unverifiable from the interior here. `[LOW]`
* **Mariana / mariana_plus / Maverick** (added AXI parity + protocol checkers to `qos_prot`): the
  PMU event/matcher set **may** expose more event sources (parity/check events) and the
  counter/matcher count **may** differ (the IP provisions 16 counters; Cayman wires 8 — a newer gen
  could wire more). The 16-bit `intr_sts` width + the `0x400` window headroom in Cayman are
  consistent with a 16-counter superset existing in another build. `[LOW]`
* The intc-trigger evidence already shows one concrete divergence: **`fis_sprot_intr[5]` is a spare
  on Cayman but "axi_checks interrupt" on maverick** (§8 CORRECTION) — a per-gen change at the FIS
  interrupt edge, OBSERVED in the trigger YAML.

> **v5 / MAVERICK caveat `[v5-interior · INFERRED]`.** The maverick `qos_*` schema copies are present
> on disk (header-OBSERVED), but the v5 *interior* semantics (any added event sources, the maverick
> `intr[5]` repurpose's exact downstream behaviour) are header-observed only — flag any v5-interior
> claim as INFERRED. **Cayman is the byte-grounded authority** for every field table on this page.

---

## 12. Reimplementation summary

For a Vision-Q7-compatible GPSIMD control plane, the `qos_pmu` + `qos_host_visible` contract a
rebuild must honour:

1. **Two observe surfaces, zero shaping control.** Both are `0x800` (2 KiB, `AddrWidth=11`) and
   purely read out the `qos_prot` shaper; neither carries a single shaping parameter. The privilege
   contract is **observe in USER, shape in SECURE** — both `qos_*` observe files are host-readable;
   `qos_prot` is secure-only.
2. **`qos_host_visible` = fixed-function monitor** (`qos_user` 52 / `nts_user` 8 / `spare_user` 4,
   60/64 RO, `IpReg` on all `qos_user` regs). It mirrors **every** `qos_prot` throttle: live NTS
   outstanding occupancy (8-bit read / 6-bit write — cap-width + 1), per-window ×2 #txn + **48-bit**
   byte counts (each with a live + a `perf_` frozen mirror), per-AXI-channel backpressure/stall
   cycles (5 ch × 2 win × 2 — order **(live_w0, perf_w0, live_w1, perf_w1)** per channel), NTS-mode
   totals, and the **signed 9-bit** delta-outstanding monitor whose underflow raises
   `fis_sprot_intr[1]`/`[3]`. Byte counters are **48-bit** even though the shaper's budgets are 36-bit
   (observe wider than you cap).
3. **`qos_pmu` = programmable debug profiler** (single `csr` bundle, 146 regs, RW-dominant). 8
   counters (IP max 16), each with a 32-bit **one-hot** `event_select`, a **64-bit** threshold, a
   dual-window **3-bit** compare op (`cmp.snap0`/`snap1`), and two **48-bit** snapshot read-outs;
   fed by 4 AXI transaction matchers (20 × 32 = **640-bit** opaque pattern CAMs). A 16-bit
   `intr_sts`/`intr_clr` (W1C) pair ORs to `fis_sprot_intr[4]`. **Boots disabled** (all-0 reset).
   Lives in the USER DEBUG-FIS leaf next to the CoreSight ELA-500.
4. **The window/snapshot config is NOT in any `qos_*` file** — it is in `fis_control.json`
   (`perf_snapshot_{lo,hi}_{0,1}` 48-bit windows, `user_fis_en`/`user_debug_en` region gates, both
   `rst=1`), which uses **decimal** offsets. The `perf_snapshot[w]` pulse is the external freeze that
   copies live → `perf_` mirrors and advances the PMU snap0/snap1 double-buffer.
5. **Opaque enums must be reverse-engineered separately**: the `event_select` one-hot bit→event map,
   the 3-bit `cmp` operator codes, and the 640-bit matcher pattern field layout are **not shipped** in
   the JSON/mako.
6. **Cayman is authoritative**: `0x800` windows, `AddrWidth=11`, all-hex sizes, 146/154 (pmu) and
   64/67 (host) geometry, node counts 528/1208 (Cayman flat map). Other generations are
   header-observed only for these two files.

---

### Confidence ledger

* **`[HIGH · OBSERVED]`** — every regfile scalar (both `0x800` / `AddrWidth=11` / `DataWidth=32` /
  all-hex sizes / no `0xb1`); both bundle maps + spans + no-overlap; the `qos_pmu` 146-reg / 154-field
  geometry (2 intr + 8×8 counters + 4×20 matchers) and the `range(8)`/`range(4)`/`range(20)` mako
  bounds; the `qos_host_visible` 64-reg / 67-field geometry (52+8+4) and its per-channel
  **(live_w0, perf_w0, live_w1, perf_w1)** layout; every per-field bit-range/reset/access/description
  in both; the RW-vs-RO split (pmu 114 RW config / host 60 RO monitor); the all-0 pmu reset (boots
  disabled); the 16-bit `intr_sts` width vs 8 instanced counters; the `pmu_counterI_cmp` "Counter 0"
  copy-paste; the 48-bit byte counters vs 36-bit `qos_prot` budgets; the 8/6-bit occupancy vs 7/5-bit
  cap; the `fis_sprot_intr[4]=qos_pmu` / `intr[1]`/`[3]=delta` trigger mapping (Cayman
  `sdma_triggers.yaml`); the `intr[5]` Cayman-spare vs maverick-axi_checks divergence; the Cayman node
  counts 1500/1208/528 (flat + xref agree); the USER FIS (qos_host_visible) vs USER DEBUG-FIS-next-to-ELA
  (qos_pmu) placement (528/528 + 1208/1208 path verification); the
  `perf_snapshot_{lo,hi}_{0,1}` + `user_fis_en`/`user_debug_en` config in `fis_control.json`; the
  Cayman-grounded provenance for the two files' interiors.
* **`[MED · INFERRED]`** — the snapshot/freeze EVENT CHAIN stitch (`perf_snapshot[w]` copies
  live→perf + advances pmu snap0/snap1) — by matching names + mako prose, no single artifact wires it;
  the "double-buffer/freeze" classification of snap0/snap1; the "match-value+mask CAM" reading of the
  640-bit matcher pattern; the `qos_prot`-mechanism → `qos_host_visible`-counter 1:1 map (by
  name/semantics); the "curated host vs flexible debug" framing; the W1C semantics of `intr_clr`
  (from prose).
* **`[LOW]`** — the `event_select` one-hot bit→event ENUM and the `cmp` 3-bit op ENUM (not shipped —
  opaque codes); the exact 640-bit matcher pattern field layout; all cross-generation
  (Sunda/Mariana/Maverick) interior expectations for these two files (header-observed only).

**Schema artifacts flagged inline** (not errors): all eight `pmu_counterI_cmp` descriptions read
"PMU Counter 0" (`${i}` miss in the generator); the intc YAML "interrrupts" triple-`r` typo is
verbatim; `fis_control.json` uses decimal `AddressOffset`s (a different regfile from the all-hex
`qos_*` files); the 8 explicit `SpecialAccess:"None"` on `nts_user` carry no special behaviour.
