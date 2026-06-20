# CSR — amzn_remapper / user_remapper (FIS sprot address/ID firewall)

The `amzn_remapper` and `user_remapper` are the **first leaf of every FIS (Fabric
Interface Slice) `sprot` region** — the address-and-master-ID protection half of the
slice, sitting in AXI series *in front of* the QoS shaper documented in
[`qos-prot.md`](./qos-prot.md). Where `qos_prot` shapes bandwidth and emits the
no-target (NTS) error response, the remapper is the **per-master egress firewall**: it
matches each outgoing AXI transaction against a content-addressable match table (CAM)
on `{masked 58-bit physical address + optional 10-bit AXI master-ID}`, allows or denies
reads and writes independently, optionally rewrites the low 46 address bits, and (on the
privileged variant only) drives the `AxPROT[2:0]` protection sideband.

The two variants are **the same IP, asymmetrically populated**, and the asymmetry *is*
the chip's core isolation primitive. The privileged `amzn_remapper` is **fail-CLOSED**
(a CAM miss blocks); the customer-facing `user_remapper` is **fail-OPEN** (a CAM miss
passes). A Vision-Q7 GPSIMD control-plane rebuild that gets this single reset value
backwards has either bricked the firmware path or torn the guest sandbox wide open.

This page is the live-CSR companion to the address-map carve in
[`../address/pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md) (§5,
the trust boundary), the SoC-fabric perimeter framing in
[`../security/soc-fabric-perimeter.md`](../security/soc-fabric-perimeter.md), the AXI
integrity sibling [`nsm.md`](./nsm.md), and the fault→isolation→IRQ flow in
[`../interrupt/nsm-flow-unified.md`](../interrupt/nsm-flow-unified.md).

> **PROVENANCE.** Every register/field/reset below is read directly from the shipped
> Cayman register schema (`csrs/sprot/amzn_remapper.json`, 89,250 B;
> `csrs/sprot/user_remapper.json`, 27,816 B — both RTL-generated, binary-derived vendor
> data and citeable as such). Counts and reset values are `jq`-computed from those two
> files; cross-generation figures come from the Sunda/Mariana arch-header twins of the
> same schema. The match/rewrite **boolean algebra** (§3) is `[MED · INFERRED]` from
> field names + semantics — the schema describes the storage, not the comparator RTL.
> Arch revision string (consts header): `cayman_golden_tapeout_candidate_2_2023_07_21`.

---

## 1. The two variants at a glance `[HIGH · OBSERVED]`

| property | `amzn_remapper` (privileged) | `user_remapper` (guest) |
|----------|------------------------------|-------------------------|
| `UnitName` / `Type` | `amzn_remapper` / `REGFILE` | `user_remapper` / `REGFILE` |
| `AddrWidth` → window | `12` → **`0x1000`** (4 KiB) | `11` → **`0x800`** (2 KiB) |
| `SizeInBytes` | `0x1000` | `0x0800` |
| `RegfileFlavor` / `InterfaceType` | `POSEDGE` / `APB` | *(keys absent)* — APB by placement |
| bundles / reg-defs / bitfield-defs | **10 / 87 / 134** | **4 / 28 / 45** |
| file size (raw bytes) | **89,250 B** | **27,816 B** |
| **CAM pass-on-miss default** | **DENY (`0x0`)** — fail-CLOSED | **PASS (`0x1`)** — fail-OPEN |
| empty-entry `id_cmp_dis` reset | `0x0` (ID-compare ON) | `0x1` (ID-compare OFF) |
| `master_prot` (AxPROT generator) | **YES** (`arprot`/`awprot` = `0x2`) | **NO** — guest cannot emit AxPROT |
| wipe peer CAM (`user_cam_ctl`) | YES | n/a |
| bypass-ID override (`wr/rd_byp_id`) | YES | NO — cannot bypass itself |
| AXI rd/wr timeout | YES | NO |
| denied-address `[57:0]` capture | YES | NO |
| AXI err / timeout 48-bit counters | YES (own **and** guest CAM) | NO |
| `delta_mon` / `bound_chk` / `txn_len_chk` | YES | NO |
| guest-CAM stats observability | YES (`user_cam_stats` @ `0x600`) | own only (`user_cam_stats` @ `0x200`) |

> **THE SIZE DELTA IS THE TRUST DELTA.** The `amzn` file is **3.2×** the `user` file
> (89,250 vs 27,816 B; 87 vs 28 register-defs; 134 vs 45 bitfields). That extra mass is
> not redundancy — it is the entire **supervisor surface**: the AxPROT generator, the
> management hooks *over the guest CAM* (wipe + bypass-ID), the timeout/denied-address
> violation machinery, and the four checker/observer bundles (errors, delta-monitor,
> boundary-check, txn-length-check) — **including a second full copy of the guest's
> stat/error counters** so firmware sees every guest pass/deny/error. The `user`
> variant carries only its own CAM, a two-register control, its own stats, and spares.

> **CORRECTION — gating discriminator is the AXI ID, not VMID.** A full-file grep of
> *both* schemas for `vmid|secure|privileged|region|domain|nonsecure` returns **zero**
> hits. The remapper gates on `{physical-address region, 10-bit AXI master-ID}` and
> emits `AxPROT`; it does **not** carry or check a VMID. VMID is assigned *upstream* in
> the DMA engine (`udma_gen_ex` VMPR/VMADDR), per queue, before the transaction reaches
> the fabric edge. The two are **disjoint, complementary** virtualization layers — VMID
> = which guest VM owns the buffer; AXI ID = which hardware master issued the bus
> transaction. Treat any "VMID gating" framing of the remapper as resolved here to
> **address-region + master-ID** gating. `[HIGH · OBSERVED]`

---

## 2. Address map (bundle layout) `[HIGH · OBSERVED]`

Register `AddressOffset` is **relative to its bundle base**. `BundleSizeInBytes` and the
regfile `SizeInBytes` are **hex strings** throughout (`tdma_model` convention); the
`bound_chk`/`txn_len_chk` bundles were authored by a different hand and use **bare-decimal**
register offsets and reset values (`"+0"`,`"+4"`,…; `"1"`,`"8"`) — unambiguous, but flagged.

```
amzn_remapper  (window 0x1000)                  user_remapper  (window 0x800)
  base    bs      #r  bundle                       base    bs    #r  bundle
  0x000  0x100   10   control   (policy/AxPROT/mgmt) 0x000  0x100  2  control  (miss + swap)
  0x100  0x100   13   amzn_cam  (the CAM, indirect)  0x100  0x100 13  user_cam (the CAM)
  0x200  0x100    9   amzn_cam_stats                 0x200  0x100  9  user_cam_stats
  0x300  0x80    12   amzn_cam_errors                0x7F0  0x10   4  spare
  0x380  0x80    12   user_cam_errors  (guest! )
  0x500  0x80     8   delta_mon                       reserved gap: 0x300..0x7F0
  0x580  0x80     5   bound_chk
  0x600  0x100    9   user_cam_stats   (guest! )
  0x700  0x80     5   txn_len_chk
  0x7F0  0x10     4   spare
  reserved: 0x400..0x500, 0x780..0x7F0; 0x800..0xFFF unused headroom
```

The `user` window is **exactly half** the `amzn` window (`0x800` vs `0x1000`), and in the
FIS `sprot` container both nest with their QoS sibling: `amzn`(`0x1000`)+`qos_prot`
(`0x1000`)=`0x2000`; `user`(`0x800`)+`qos_host_visible`(`0x800`)=`0x1000`. No bundle
overlaps (computed). The `amzn` file carries `user_cam_stats`/`user_cam_errors` so the
**privileged observer accounts the guest CAM**.

---

## 3. The remap CAM — match → rewrite (byte-exact) `[HIGH · OBSERVED fields]`

The CAM is **not** a flat array of entry registers. It is accessed **indirectly** through
a read-window and a write-window. One logical entry = **six 32-bit words = 192 bits**.
The `amzn_cam` and `user_cam` bundles are **byte-identical in structure** (13 registers:
`rd_idx`, `rd_buf_0..5` RO, `wr_buf_0..5` WO) — they differ in exactly **one reset value**
(`rd_buf_1.id_cmp_dis`, §3.5).

### 3.1 Indirect entry-access protocol `[HIGH · widths; MED · semantics]`

```
READ  entry N:  rd_idx.rd_index[6:0] = N   →  rd_buf_0..5 present entry N (6 RO words)
WRITE entry N:  fill wr_buf_0..4;  wr_buf_5.wr_index[31:24] = N  →  commits entry N
```

`rd_index` is **7-bit** (`[6:0]`, addressing ceiling 128); `wr_index` is **8-bit**
(`[31:24]`). The **actual** CAM entry depth is a synthesis parameter and is **not encoded
in any shipped artifact** — the addressing ceiling is 128 but the synthesized depth is
unknown. `[HIGH · index widths; LOW · INFERRED actual depth ≤ 128]`

### 3.2 Per-entry word layout (read-back view, `rd_buf_*`) `[HIGH · OBSERVED]`

| word | bits | field | meaning |
|------|------|-------|---------|
| `rd_buf_0` | `[31:0]` | `cmp_addr` | Compare Address `[31:0]` |
| `rd_buf_1` | `[31]` | `valid` | entry participates in the match (1=valid) |
| `rd_buf_1` | `[30]` | `wr_pass` | on hit: 1 = write txns PASS, 0 = DENY |
| `rd_buf_1` | `[29]` | `rd_pass` | on hit: 1 = read txns PASS, 0 = DENY |
| `rd_buf_1` | `[28]` | `id_cmp_dis` | 1 = ignore AXI ID (address-only match) |
| `rd_buf_1` | `[27]` | `intlv_en` | 1 = apply the interleave bit-swap (§4.4) |
| `rd_buf_1` | `[26]` | `remap_en` | 1 = rewrite the address; 0 = pass-through |
| `rd_buf_1` | `[25:0]` | `cmp_addr` | Compare Address `[57:32]` |
| `rd_buf_2` | `[31:0]` | `cmp_addr_mask` | Compare Address Mask `[31:0]` |
| `rd_buf_3` | `[25:0]` | `cmp_addr_mask` | Compare Address Mask `[57:32]` |
| `rd_buf_4` | `[31:0]` | `remap_addr` | Remap (output) Address `[31:0]` |
| `rd_buf_5` | `[25:16]` | `id` | AXI ID to match — **10-bit** master-ID |
| `rd_buf_5` | `[13:0]` | `remap_addr` | Remap Address `[45:32]` |

So `cmp_addr` and `cmp_addr_mask` are each **58-bit** (`[57:0]`), spanning two words;
`remap_addr` is **46-bit** (`[45:0]`); `id` is **10-bit**. The write view (`wr_buf_*`)
is identical except `wr_buf_5` packs `wr_index[31:24]`, `id[23:14]`, `remap_addr[13:0]`
(the read-back surfaces `id` at `[25:16]`, the write side at `[23:14]` — same 10 bits,
different lane).

> **NOTE — the 58-bit compare is the full SoC physical address.** `cmp_addr[57:0]` maps
> exactly onto the SoC address layout: `LOCAL[46:0]` + `DIE[47]` + `CAYMAN_ID[53:48]` +
> `CAYMAN_ID_VALID[54]` + `RESERVED[55]` + `PCIE_ATTR_RELAXED_ORDERING[56]` +
> `OK_TO_FAIL[57]`. A single masked CAM compare can therefore region-match **by die, by
> mesh chip-id, by PCIe attribute, by ok-to-fail, or by local byte range** at once.

### 3.3 The match rule `[MED · INFERRED from field semantics]`

```
hit(entry) =  valid
           && ((incoming_addr ^ cmp_addr) & ~cmp_addr_mask) == 0      // masked region match
           && (id_cmp_dis || ((incoming_id ^ id) == 0))               // optional 10-bit AXI-ID qual
```

A classic TCAM region match (mask bit `1` = don't-care) with an **optional master-ID
qualifier**. When `id_cmp_dis = 1` the entry matches purely on the masked address; when
`0`, the incoming 10-bit AXI ID must equal the entry `id`. The master-ID space is **1024
masters** (10-bit) on Cayman.

### 3.4 The rewrite / action rule `[HIGH · widths; MED · INFERRED rule]`

```
on hit:
  read  txn  → rd_pass ? ALLOW : DENY
  write txn  → wr_pass ? ALLOW : DENY
  if remap_en:  out_addr[45:0]  = remap_addr[45:0]    // low 46 bits replaced
                out_addr[57:46] = incoming_addr[57:46] // upper 12 bits unchanged
  if intlv_en:  swap address bits swap_bit_0 <-> swap_bit_1   (§4.4)
```

> **QUIRK — compare is 58-bit, remap is 46-bit (deliberate asymmetry).** The comparator
> sees all 58 address bits but the rewrite only replaces the bottom **46** (`LOCAL` minus
> bit 46). Remap therefore relocates a base **within a die / within a region** and can
> *never* rewrite `DIE[47]` or `CAYMAN_ID[53:48]`. It is an intra-die base relocation,
> **not** a cross-die reroute. `[HIGH · widths · OBSERVED; MED · INFERRED interpretation]`

A **miss** does *not* use the entry verdict — it falls through to `control.pass_on_miss`
(§4.1). A **deny** (from a hit with `*_pass=0`, or a fail-closed miss, or a checker)
terminates the transaction and raises the FIS `sprot` interrupt (§6).

### 3.5 The one-line CAM diff — empty-entry ID policy `[HIGH · OBSERVED]`

```
amzn_cam.rd_buf_1.id_cmp_dis  reset = 0x0   → a reset/empty AMZN entry: ID-compare ENABLED
user_cam.rd_buf_1.id_cmp_dis  reset = 0x1   → a reset/empty USER entry: ID-compare DISABLED
```

This is the *only* structural reset difference between the two CAM bundles (verified by a
sorted field diff). It reinforces the trust split: an empty privileged entry defaults to
**strict** (must match the master-ID); an empty guest entry defaults to **address-only**.

---

## 4. The `control` bundle — miss policy, AxPROT, management `[HIGH · OBSERVED]`

### 4.1 Pass-on-miss — THE central trust boundary `[HIGH · OBSERVED]`

This is the chip's core isolation primitive, pinned **byte-for-byte from both JSONs**:

| variant | register `@0x0` | field | `Position` | **`ResetValue`** | miss verdict |
|---------|-----------------|-------|------------|------------------|--------------|
| `amzn_remapper` | `amzn_cam_pass_on_miss` | `rd_pass_on_miss` | `4` | **`0x0`** | reads that miss → **DENY** |
| `amzn_remapper` | `amzn_cam_pass_on_miss` | `wr_pass_on_miss` | `0` | **`0x0`** | writes that miss → **DENY** |
| `user_remapper` | `user_cam_pass_on_miss` | `rd_pass_on_miss` | `4` | **`0x1`** | reads that miss → **PASS** |
| `user_remapper` | `user_cam_pass_on_miss` | `wr_pass_on_miss` | `0` | **`0x1`** | writes that miss → **PASS** |

The schema `Description` says it verbatim: *"0 — Reads/Writes that miss in the AMZN/User
CAM are marked Deny; 1 — … marked Pass."*

> **WALL — `amzn` = fail-CLOSED (`0x0`), `user` = fail-OPEN (`0x1`). This is the
> isolation primitive.** The privileged firmware path is **whitelist-by-default**: any
> transaction not explicitly admitted by an `amzn_cam` entry is *denied*. The guest path
> is **allow-by-default**: a transaction that misses the `user_cam` *passes through*,
> until firmware tightens it (by populating entries, flipping the bit, or wiping the
> guest CAM via §4.3). For a reimplementer this means two things must be true at reset:
> the privileged remapper boots **deny-by-default** (so a half-configured boot cannot
> leak), and the guest remapper boots **permissive** (so an un-provisioned guest is not
> bricked — the guest is *contained* by the surrounding privileged firewall and the QoS
> NTS error path, not by its own default-deny). Inverting either value is a security
> defect: `amzn`→fail-open removes the firmware whitelist entirely; `user`→fail-closed
> deadlocks every un-provisioned guest master. These resets are **frozen across
> Sunda / Cayman / Mariana** (§7) — a deliberately immutable invariant.

This matches [`../address/pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md)
§5 (`amzn-fail-CLOSED / user-fail-OPEN`, `pass_on_miss` `0x0`/`0x1`) **byte-for-byte**; no
correction. *(That page's "408 / 612" figures are pkl address-map **leaf-record** counts,
a different axis from the **register-definition** counts 87 / 28 here — not a divergence.)*

### 4.2 `master_prot` — the AxPROT generator (AMZN-only) `[HIGH · OBSERVED]`

```
control.master_prot @0x10  (RW)
  arprot  [6:4]  reset = 0x2   "ARPROT default value"  driven onto read transactions
  awprot  [2:0]  reset = 0x2   "AWPROT default value"  driven onto write transactions
```

This is the `AxPROT[2:0]` privileged/secure/instruction protection sideband that the QoS
sibling (`qos_prot`) does **not** emit. Reset `0x2` sets bit `[1]` only → in the ARM AxPROT
encoding (`[0]`=privileged, `[1]`=non-secure, `[2]`=instruction) that is **non-secure,
privileged, data** access. `[HIGH · OBSERVED field/reset; MED · INFERRED ARM encoding]`

> **AMZN-ONLY — the guest cannot forge AxPROT.** `user_remapper` has **no** `master_prot`
> register. A guest CAM can pass/deny/remap, but it physically **cannot set or override**
> the protection bits on its outgoing transactions. A Vision-Q7 rebuild must never expose
> `arprot`/`awprot` writability on the user side — doing so would let a guest claim
> privileged/secure attributes on the fabric.

### 4.3 Guest-CAM management hooks (AMZN-only) `[HIGH · OBSERVED]`

| register | `@off` | key field(s) | reset | effect |
|----------|--------|--------------|-------|--------|
| `user_cam_ctl` | `0x20` | `cam_clr[0]` (WO) | `0x0` | write 1 → **restore the entire guest CAM to reset state** (firmware wipe) |
| `user_cam_wr_byp_id` | `0x30` | `wr_byp_id_en[31]` | `0x1` | enable write-bypass ID compare |
| | | `wr_byp_id_mask[25:16]` | `0x0` | per-bit don't-care over the bypass ID |
| | | `wr_byp_id[9:0]` | `0x0` | if `(incoming_id & ~mask)==wr_byp_id` the **write bypasses the guest CAM** |
| `user_cam_rd_byp_id` | `0x34` | `rd_byp_id_en[31]` / `rd_byp_id_mask[25:16]` / `rd_byp_id[9:0]` | `0x1`/`0x0`/`0x0` | same, for reads |

> **THE TRUST BOUNDARY IS HIERARCHICAL.** The guest populates `user_cam`, but **firmware
> holds the overrides**: it can wipe the guest CAM (`cam_clr`) and nominate a masked AXI
> master-ID whose reads/writes **skip the guest policy entirely** (`wr/rd_byp_id`) — e.g.
> a trusted DMA master that firmware exempts from the guest's gating. The guest cannot
> reach these registers (they live only in the privileged `amzn` window) and cannot
> bypass *itself*. The `*_byp_id_en` enable bits at `[31]` (reset `0x1`) are **new in
> Cayman** vs Sunda (§7).

### 4.4 Shared control — timeouts, denied-addr, interleave-swap `[HIGH · OBSERVED]`

`amzn`-only timeout + violation latch:

```
axi_rd_timeout @0x50 / axi_wr_timeout @0x54   axi_tout_cnt[31:0] RW rst=0x0
    AXI clocks an access may stay active before timeout (0x0 => 2^32 => effectively off)
addr_denied_lo @0x60  addr[31:0]  RO          // captured [57:0] address of the most-recent
addr_denied_hi @0x64  addr[25:0]  RO          //   DENIED transaction — the ISR's violation latch
```

Present in **both** files (`+0x40`):

```
amzn_interlv_swap (amzn) / user_interlv_swap (user)
    swap_bit_1[13:8] RW rst=0x0   bit position to exchange with swap_bit_0
    swap_bit_0[5:0]  RW rst=0x0   bit position to exchange with swap_bit_1
```

When a hit entry has `intlv_en=1`, the two named bit positions are **exchanged** in the
outgoing address — a cheap memory-interleave / bank-swap across a stripe. The 6-bit
positions (0..63) cover the 58-bit address. Each block carries its own swap config.

---

## 5. Checkers, stats, observability (AMZN-side) `[HIGH · OBSERVED]`

Beyond the CAM, the privileged remapper enforces three parallel AXI checks and keeps
per-direction telemetry. **None of these exist in `user_remapper`** — the guest has only
its CAM, the miss/swap control, and its own pass/deny stats.

- **`{amzn,user}_cam_stats`** (9 regs each) — 48-bit `{rd,wr}×{pass,deny}` counters
  (`*_lo[31:0]` + `*_hi[15:0]`), plus `clear_stats.clear[0]` (WO, `SpecialAccess=PulseOnW`).
  The `amzn` file holds **both** its own counters (`@0x200`) **and** the guest's (`@0x600`).
- **`{amzn,user}_cam_errors`** (12 regs each, `amzn` file only) — 48-bit AXI read/write
  error-response counts, read/write timeout counts, and timeout-monitor (`tout_occur` /
  `tout_comp`) telemetry. The privileged observer accounts errors for *both* CAMs.
- **`delta_mon`** (8 regs) — an AXI handshake-balance monitor over `{aw_minus_b,
  aw_minus_w, ar_minus_r, outstanding_r_beat, outstanding_w_beat}` (enables reset `1`).
  `error.aw_minus_b_underflow[0]` / `ar_minus_r_underflow[4]` fire when responses exceed
  requests (a protocol anomaly / spurious-response fault) — these two are dedicated
  interrupt sources (§6).
- **`bound_chk`** (5 regs) — 4 KiB-(or finer)-boundary-crossing guard. `cfg`:
  `wr_en/rd_en` reset `1`, `default_wr/rd_pass` reset **`0`** (a burst that straddles the
  boundary is **denied** by default), `wr/rd_granularity[…]` reset **`8`** = `2^8` = 256-byte
  boundary (valid `2..12` = 4 B..4 KiB).
- **`txn_len_chk`** (5 regs) — AXI burst-**length** cap. `cfg`: `wr_en/rd_en` reset `1`,
  `txn_awlen_max[7:4]` / `txn_arlen_max[11:8]` reset **`0x7`** (8-beat burst); `AWLEN`/`ARLEN`
  above the max is denied. **This is the one bundle Cayman adds over Sunda** (§7).
- **`spare`** (4 regs, both files) — `zeros_0/1` reset `0x0`, `ones_0/1` reset `0xFFFFFFFF`.

> **NOTE — `0xb1` reset placeholder is absent.** Both files grep clean for the unbound
> `0xb1` placeholder (`grep -ci '0xb1' = 0`): every reset value below is concrete, not a
> generator stub. `[HIGH · OBSERVED]`

---

## 6. Protection verdict → interrupt tie `[HIGH · OBSERVED]`

Per AXI transaction the remapper resolves:

1. **CAM match** (masked 58-bit address + optional 10-bit AXI-ID).
2. **HIT** → `(rd_pass,wr_pass)` allow/deny per direction; optional remap + interleave;
   AxPROT default from `master_prot`. **MISS** → `pass_on_miss` verdict (§4.1).
3. In parallel: `bound_chk` (boundary span), `txn_len_chk` (burst length), AXI timeout.
4. Any **DENY** terminates the transaction, latches the `[57:0]` address into
   `addr_denied_lo/hi`, increments the deny counter, and raises the FIS `sprot` interrupt.

The FIS `sprot` interrupt is a 6-entry vector (`fis_sprot_intr[5:0]`, all `edge_triggered`,
`needs_cdc:false`); three sources are remapper-owned:

| idx | source | owner |
|-----|--------|-------|
| `[0]` | "amzn_remapper denied a transaction" | CAM / policy **DENY** |
| `[1]` | "delta monitor — R responses > AR requests" | `delta_mon.ar_minus_r_underflow` |
| `[2]` | "tmu detected an AXI timeout" | `axi_rd/wr_timeout` |
| `[3]` | "delta monitor — B responses > AW requests" | `delta_mon.aw_minus_b_underflow` |
| `[4]` | "qos pmu interrupt (OR of all 16 PMU counters)" | `qos_pmu` |
| `[5]` | `fis_sprot_spare_0` | spare |

> **NOTE — there is no separate `user_remapper` deny interrupt.** A guest-CAM deny is
> surfaced through the **same** `amzn_remapper`-named `sprot_intr[0]` line (the
> address-map alias `amzn_il`/`user_il`, where `il` = *isolation layer*, is the remapper).
> So the privileged ISR sees **both** privileged and guest denials.
> `[HIGH · trigger names; MED · INFERRED guest-deny routing]`

> **NOTE — the remapper DECIDES, qos_prot RESPONDS.** The remapper schema encodes no AXI
> response code for a deny (no `read_response`/`write_response` field). The deny
> terminates the transaction and is *counted*; the actual `SLVERR`/`DECERR` response (and
> the `0xDEADBEEF` read-data poison) on a denied / no-target path is emitted by the
> sibling [`qos_prot`](./qos-prot.md) NTS block. The FIS `sprot` block splits roles:
> **remapper = decide (pass/deny/remap/AxPROT); QoS NTS = respond (error + poison).**
> `[HIGH · split; MED · INFERRED deny-uses-NTS-path]`

---

## 7. Cross-generation — frozen core, widened ID `[HIGH · OBSERVED]`

The CAM IP is **stable from Sunda**; Cayman is the intermediate that widened the master-ID
space and added the burst-length checker; Mariana is a later control-bundle hardening pass.

| | Sunda | **Cayman** | Mariana / + |
|---|------:|----------:|------------:|
| `amzn` bundles / reg-defs / bitfield-defs | 9 / 82 / 120 | **10 / 87 / 134** | 10 / 131 / 259 |
| `user` bundles / reg-defs / bitfield-defs | 4 / 28 / 45 | **4 / 28 / 45** | 4 / 65 / 158 |
| `amzn` / `user` window | 0x1000 / 0x800 | 0x1000 / 0x800 | 0x1000 / 0x800 |

**Sunda → Cayman deltas** (verified): (a) AMZN **+1 bundle** `txn_len_chk`; (b) `amzn_cam`
`id` widened **8→10 bit** (`rd_buf_5` `[23:16]`→`[25:16]`); (c) `user_cam_wr/rd_byp_id`
widened 8→10 bit **and** Cayman **adds** the `wr/rd_byp_id_en[31]` match-enable bits
(reset `0x1`). **User:** `user_cam.id` widened 8→10 bit; empty-entry `id_cmp_dis` read-back
default `0x0`→**`0x1`** (Cayman makes a reset guest entry ID-agnostic).

> **FROZEN INVARIANTS (Sunda = Cayman = Mariana):** the `pass_on_miss` fail-closed/fail-open
> split (`0x0`/`0x1`), the `master_prot` AxPROT default (`0x2`), and the CAM bundle
> structure (13 regs / 28 fields) are **byte-identical across all three generations**.
> Only the `id` field *width* moved. The Mariana growth is entirely in the `control`
> bundle (AMZN `control` 10→52 regs, USER 2→39, an AXI-parity/protocol-check family) bolted
> onto this frozen core.

> **WALL — v5 / MAVERICK is header-OBSERVED only.** The Cayman schema above is the
> byte-grounded anchor. Where the Maverick (NC-v5) twin is referenced (e.g. via
> [`../address/pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md)), the
> **schema structure, names, sizes and reset values are `[HIGH · OBSERVED]`** (RTL-derived
> JSON, citeable), but any statement about **what the v5 silicon does at runtime** behind
> a given remapper leaf is `[* · INFERRED]`. The enforcement-core reset values
> (`pass_on_miss`, AxPROT) are reported byte-identical Cayman↔Maverick.

---

## 8. Reimplementation checklist `[summary]`

- **Reset the privileged remapper fail-CLOSED** (`amzn_cam_pass_on_miss.rd/wr = 0x0`) and
  the **guest remapper fail-OPEN** (`user_cam_pass_on_miss.rd/wr = 0x1`). Never invert.
- **Only the privileged variant emits AxPROT** (`master_prot.arprot/awprot = 0x2`,
  non-secure-privileged-data); the guest variant has no `master_prot` and must never
  acquire one.
- **CAM entry = 6 words / 192 bits**, indirect-accessed (`rd_idx` → `rd_buf_*`;
  `wr_buf_*` + `wr_buf_5.wr_index` commit). Match = masked 58-bit address (mask `1`=
  don't-care) AND optional 10-bit AXI-ID. Rewrite replaces only `out_addr[45:0]`.
- The **management hooks over the guest CAM** (`user_cam_ctl` wipe, `wr/rd_byp_id`
  override) live **only** in the privileged window — the supervisor reaches *into* the
  guest, never the reverse.
- A **deny** is decided here, latched in `addr_denied_*`, counted, and signalled on
  `fis_sprot_intr[0]`; the **error response** is the QoS NTS block's job, not the
  remapper's.
- The remapper gates on **address-region + AXI master-ID**, *not* VMID — keep that
  layering distinct from the upstream DMA-engine VMID assignment.
