# CSR — `rdm_model` + `top_sp_ram`

Two control blocks that both carry the word *top-level descriptor* in their lineage
yet sit in unrelated subsystems. They are documented together because a
control-plane rebuild touches both at once: **`rdm_model`** is the INTC-side
**Ring-Descriptor-Manager** — the engine that writes completed-descriptor *tail
pointers* back to host/HBM ring memory; **`top_sp_ram`** is the per-instance config
surface of **TOP_SP**, the top-level service/sync processor — an Xtensa-NX scalar
core with an event/semaphore `tsync` substrate that doubles as collective-sequencer
**engine 5**.

Both register files are extracted byte-exact from the Cayman arch-regs schema
(`csrs/rdm/rdm_model.json`, `csrs/top_sp/top_sp_ram.json`). Every numeric claim
below is grounded in the JSON itself. The page default is
**`[HIGH · OBSERVED]`**; claims that depart from it carry an explicit
**HIGH / MED / LOW** × **OBSERVED / INFERRED / CARRIED** tag
(CARRIED = imported from a committed sibling page, not present in these two JSONs).

Related: [TOP_SP collective lowering](../../collectives/ops/top-sp-lowering.md) ·
[EVT_SEM address regions](../address/evt-sem-regions.md) ·
[`pring` descriptors](../../collectives/ncfw/pring-descriptors.md) ·
[PEB / CC / TOP_SP triggers](../interrupt/peb-cc-topsp-triggers.md) ·
[UDMA S2M](udma-s2m.md) ·
[`pkl` TOP_SP coverage](../address/pkl-topsp-coverage.md).

---

## 1 · RegFile-level facts (both files)

Both are 4 KiB (`AddrWidth = "12"` ⇒ a `0x1000` window), 32-bit-data, POSEDGE,
APB regfiles with exactly **one** bundle array (`ArraySize = "1"`).

| Field | `rdm_model` | `top_sp_ram` |
|---|---|---|
| `UnitName` | `rdm_model` | `top_sp_ram` |
| `Type` / `RegfileFlavor` | `REGFILE` / `POSEDGE` | `REGFILE` / `POSEDGE` |
| `DataWidth` / `AddrWidth` | `"32"` / `"12"` (strings) | `"32"` / `"12"` (strings) |
| window (`SizeInBytes`) | `"0x1000"` (4 KiB) | `"0x1000"` (4 KiB) |
| `InterfaceType` | `APB` | `APB` |
| bundle name | `rdm` | `top_sp_ram_cfg` |
| bundle `AddressOffset` | `"0"` (decimal) | `"0x0"` (hex) |
| bundle `BundleSizeInBytes` | `"0x1000"` | `"0x1000"` |
| register defs | **236** | **8** |
| bitfield defs | **242** | **11** |

> **SCHEMA GOTCHA — radix differs *per file*.** `SizeInBytes` /
> `BundleSizeInBytes` / `DataWidth` / `AddrWidth` are **hex/decimal strings**, not
> ints, in *both*. But the per-register `AddressOffset` radix is **not the same
> between the two files**:
> `rdm_model` offsets are **decimal** strings (`"0","4","8",…,"940"`) while
> `top_sp_ram` offsets are **hex** strings (`"0x0","0x4",…,"0x900","0x904"`).
> A parser that assumes one radix will mis-place every register in one of the two
> files. Verified against the raw `AddressOffset` strings in each.

> **NOTE — no `0xb1` reset placeholder.** Some arch-regs files seed unfilled reset
> values with the sentinel `0xb1`; `grep -ci 0xb1` = **0** in both of these. Every
> reset value quoted below is a real silicon default, not a placeholder.

### 1.1 · Worked bundle-base computation

Each leaf register's absolute address is
`leaf_base + bundle.AddressOffset + register.AddressOffset`
(the bundle `ArraySize` is 1, so there is no per-element stride to fold in).

**RDM** — Cayman absolute base of `INTC_RDM_CTRL` is `0x000008006C83000`
(see §3). Bundle `rdm` is at `+0`. So:

```
control          (reg off 904 = 0x388) → 0x000008006C83388
queue_size_5     (reg off 32+5·36 = 212 = 0xD4) → 0x000008006C830D4
```

**TOP_SP** — Cayman `TOP_SP_0` NODE base `0x8280000000`, RAM window at `+0x100000`,
bundle `top_sp_ram_cfg` at `+0x0` of RAM:

```
top_sp_ram_cfg base → 0x8280100000
timestamp_inc (off 0x4)  → 0x8280100004
sw_queue      (off 0x10) → 0x8280100010
```

---

## 2 · RDM — the Ring-Descriptor-Manager

### 2.1 · Identity & write-only model `[HIGH · OBSERVED for mechanism; MED · INFERRED for the name expansion]`

`rdm_model` is a 4 KiB APB block that lives **inside the INTC subsystem** (address
node `INTC_RDM`, *not* an SDMA channel). For **24 queues** it holds, per queue, a
host-/HBM-memory **descriptor-ring base** plus a **tail-pointer write-back AXI
address** — separately for the M2S (outbound) and S2M (inbound) ring halves — plus
a ring size; and a control/status tail that manages a linked-list of in-flight AXI
write IDs, per-descriptor `ringId` field injection, and AXI write-response
(B-channel) error handling.

The defining architectural fact: **RDM issues writes only.** Its AXI *read*
auto-responder is permanently clock-gated —

> `ctrl.clkgate_req` (reg off 920, bit 0) **resets to 1**, described as
> *"Clockgate enable for the AXI read auto-responder NTS. This needs to be asserted
> always since RDM does not implement reads."*

So RDM is a pure descriptor **write-back / notification** engine: it produces tail
pointers and `ringId` fields and pushes them out over AXI; it never reads
descriptor memory. The name "Ring Descriptor Manager" is **inferred** from the
register/trigger semantics (no shipped string spells the acronym), but every
register and every interrupt is ring / descriptor / linked-list / `bvalid`
(write-response) semantics, so the *mechanism* is HIGH even where the *name* is MED.

`rdm_model.json` is **byte-identical** across cayman / mariana / mariana_plus /
sunda (`jq -S` sorted diff = empty for all three vs cayman).

### 2.2 · Per-queue register block — the ring & tail-pointer model

The 216 per-queue registers are **9 regs × 24 queues** on a **36-byte stride**
(queue *i* occupies decimal offsets `i·36 … i·36+32`). The Cayman JSON carries
**blank `BitField.Description`** on these; the field *semantics* below are taken
from the sibling C header `cayman/rdm.h` (same field layout, identical widths /
masks — `DATA_MASK 0xfffff` ⇒ 20 bits etc. — `jq`-cross-checked), which is itself
binary-derived from the arch-regs tarball.

| reg (queue 0) | off (dec) | field width | reset | meaning (`cayman/rdm.h`) |
|---|---|---|---|---|
| `queue_base_lo_m2s_0` | 0 | `data` `[19:0]` | 0 | AXI base address **[31:12]** of M2S ring 0 |
| `queue_base_hi_m2s_0` | 4 | `data` `[25:0]` | 0 | AXI base address **[57:32]** of M2S ring 0 (⇒ 57-bit ring base) |
| `queue_tail_ptr_lo_m2s_0` | 8 | `addr` `[31:0]` | 0 | AXI address **[31:0]** of the M2S tail-ptr write-back slot |
| `queue_tail_ptr_hi_m2s_0` | 12 | `addr` `[25:0]` | 0 | AXI address **[57:32]** of the M2S tail-ptr WB slot |
| `queue_base_lo_s2m_0` | 16 | `data` `[19:0]` | 0 | same, S2M (inbound) ring base [31:12] |
| `queue_base_hi_s2m_0` | 20 | `data` `[25:0]` | 0 | S2M ring base [57:32] |
| `queue_tail_ptr_lo_s2m_0` | 24 | `addr` `[31:0]` | 0 | S2M tail-ptr WB slot addr [31:0] |
| `queue_tail_ptr_hi_s2m_0` | 28 | `addr` `[25:0]` | 0 | S2M tail-ptr WB slot addr [57:32] |
| `queue_size_0` | 32 | `data` `[21:0]` | **`10`** (`0xa`) | *"queue size in terms of 4 16B-descriptors. M2S and S2M queues must have the same size"* |

**Address composition.** A ring base is a **57-bit** AXI address split
`[57:32]:[31:12]` — i.e. the low 12 bits are implicitly zero (rings are 4 KiB-page
aligned). A tail-pointer write-back slot is a **57-bit byte address** split
`[57:32]:[31:0]` (full 32-bit low half; no page alignment forced on the WB slot).
**`queue_size`** is counted in units of **four 16-byte descriptors = 64 B**
(reset `0xa` ⇒ 10 such blocks = 640 B = 40 descriptors); the constraint is that the
M2S and S2M halves of a queue share one size.

> **NOTE — `queue_base_hi` resets encode the queue index (queues 1–11 only).**
> Reading the JSON's `ResetValue` across all 24 queues shows a striped default:
> for queues **1…11**, `queue_base_hi_m2s_i` resets to **`i`** and
> `queue_base_hi_s2m_i` resets to **`2·i`** (e.g. m2s_5 = 5, s2m_5 = 10;
> m2s_11 = 11, s2m_11 = 22). Queues **0** and **12…23** reset to **0**. This is a
> power-on default that seeds distinct high-address bits per ring so an unconfigured
> RDM never aims two queues at the same HBM page; firmware is still expected to
> program real bases before enabling a queue. Surfaced here because these resets
> are commonly treated as uniformly zero.

### 2.3 · Control / status tail

The 20 control/status registers occupy decimal offsets **864 … 940**. Unlike the
queue block, these **do** carry descriptions in the Cayman JSON.

| reg | off (dec / hex) | access | fields (Position) | reset | role |
|---|---|---|---|---|---|
| `same_id` | 864 / `0x360` | RW | `pointer[7:0]`, `queue[15:8]`, `enable[16]` | 0 | selector to inspect one `(queue,pointer)` linked-list node |
| `queue_enable` | 868 / `0x364` | RW | `bitmap[23:0]` | 0 | *"enable a queue, one bit per queue"* |
| `queue_disabled_drop_count` | 872 / `0x368` | RO | `bitmap[31:0]` | 0 | per-queue dropped-while-disabled count |
| `bresp_value` | 876 / `0x36C` | RW | `status[31:0]` | **`0xffffffff`** | B-response returned to writes while disabled (2 bits per slot) |
| `bresp_value_1` | 880 / `0x370` | RW | `status[31:0]` | **`0xffffffff`** | B-response for the upper slots (2 bits per slot) |
| `spare_rw_0` / `spare_rw_1` | 884 / 888 | RW | `data[31:0]` | 0 | spare RW |
| `spare_ro_0` / `spare_ro_1` | 892 / 896 | RO | `data[31:0]` | 0 | spare RO |
| `sta_ids_available` | 900 / `0x384` | RO | `data[31:0]` | 0 | bitmap of free AXI-write-transaction IDs in the linked-list pool |
| `control` | 904 / `0x388` | RW | see below | 0 | **tail-pointer write gate + linked-list pop control** |
| `sta_is_pointer_write` | 908 / `0x38C` | RO | `data[31:0]` | 0 | status: in-progress pointer write |
| `sta_debug` | 912 / `0x390` | RO | `data[31:0]` | 0 | debug status |
| `sta_queue_head` | 916 / `0x394` | RO | `data[31:0]` | 0 | *"contents of the head node"* (linked-list head) |
| `ctrl` | 920 / `0x398` | RW | `clkgate_req[0]` | **`1`** | clock-gate the (unused) read auto-responder — §2.1 |
| `ringid_enable` | 924 / `0x39C` | RW | `bitvector[23:0]` | **`1`** | per-queue: when 1, RDM **overwrites** the descriptor `ringId` field |
| `ringid_initial` | 928 / `0x3A0` | RW | `value[31:0]` | **`0x55555555`** | 2 bits/queue, initial `ringId` (queues 0–15) |
| `ringid_initial_1` | 932 / `0x3A4` | RW | `value[15:0]` | **`0x5555`** | 2 bits/queue, initial `ringId` (queues 16–23) |
| `ringid_reset` | 936 / `0x3A8` | RW | `bitvector[23:0]` | 0 | write with bit set ⇒ reset that queue's `ringId` to its initial value |
| `sta_debug_1` | 940 / `0x3AC` | RO | `data[4:0]` | 0 | debug status (5-bit) |

**`control` (off 904) bitfields — the tail-pointer write-back gate:**

| bit | field | description (JSON) |
|---|---|---|
| 0 | `tail_ptr_write_disable_force` | disable tail-pointer writes — used after an error to freeze WB while forcing linked-list pops |
| 1 | `tail_ptr_write_disable_auto` | on a write-response **error**, auto-disable tail-pointer writes |
| 2 | `tail_ptr_write_disable_clear` | clear the internal auto-disable register |
| 3 | `pop_one` | pop the head of the queue if non-empty |
| 4 | `pop_all` | pop entries while the queue is non-empty |

> **CORRECTION — `queue_disabled_drop_count` is 32-bit.** It is sometimes listed
> as `[23:0]`. The JSON bitfield is `bitmap[31:0]` —
> a **32-bit** field. (The *queue* count is still 24; the upper byte is simply not
> a per-queue bit.) Corrected here.

> **CORRECTION — the `bresp_value` slot packing is header-derived, not in the JSON.**
> `bresp_value` / `bresp_value_1` are commonly glossed as *"Two bits per 128B slot.
> Slot0(queue0)=bits 1:0 … slots 16..23"*. That gloss originates in the sibling
> header, **not** in
> `rdm_model.json`, whose bitfield is a flat `status[31:0]` with **blank
> Description**. The "2 bits per slot" packing is consistent with the 24-queue ×
> 2-bit B-response model but is INFERRED, not literal from this JSON. Both reset to
> `0xffffffff` (all-`0b11`). `[bitfield+reset HIGH · OBSERVED; slot packing MED · INFERRED]`

### 2.4 · The write-back path, end to end `[HIGH · OBSERVED for primitives; MED · INFERRED for the assembled flow]`

1. Host/firmware programs, per queue, the 57-bit **ring base** (`queue_base_*`) and
   the 57-bit **tail-pointer write-back slot** (`queue_tail_ptr_*`), and the shared
   `queue_size`; then sets the queue's bit in `queue_enable`.
2. As descriptors are produced, RDM **writes the updated tail pointer** to that
   queue's WB slot over AXI. If `ringid_enable[i]` is set, it also **overwrites the
   2-bit `ringId`** field in the descriptors it emits, seeded from
   `ringid_initial` / `ringid_initial_1` and resettable via `ringid_reset`.
3. Each in-flight write is tracked by an **AXI-write-ID** drawn from a linked-list
   pool; `sta_ids_available` is the free-ID bitmap and `sta_queue_head` exposes the
   head node. `same_id` selects a `(queue,pointer)` node for inspection.
4. On the AXI **B-channel** (write response): a B-valid for an ID that is not
   pending, is free, or arrives on an empty list, plus genuine write errors / bad
   write-strobes / spurious responses, are the RDM interrupt causes (§2.5). A real
   error can **auto-freeze** tail-pointer writes (`control.tail_ptr_write_disable_auto`)
   so firmware can drain the list via `pop_one` / `pop_all` without further
   corrupting host memory.

This is the same AXI *descriptor-ring + tail-pointer write-back* idiom the SDMA
[UDMA S2M](udma-s2m.md) completion engine uses; RDM is the **INTC-side instance**
of that idiom (it writes back the notification / MSI-X descriptor rings the INTC
produces), *not* a tensor-DMA or collective-reduce datapath.

### 2.5 · RDM 64-cause interrupt set

`aws_intc_rdm_count = 64`. Causes 0–8 are functional; 9–63 are FIS/security-protect
(`sprot_intr_*`) spares. Every functional cause is an AXI-write-response or
linked-list condition — corroborating the write-only model of §2.1/§2.4:

| cause | name | condition |
|---|---|---|
| 0 | `intr_bvalid_id_not_expected` | B-valid for an ID not pending |
| 1 | `intr_bvalid_id_not_free` | B-valid for an ID that is free |
| 2 | `intr_bvalid_not_empty` | B-valid when the linked list is empty |
| 3 | `intr_linked_list_overflow` | list depth > total IDs |
| 4 | `intr_bvalid_err` | a write returned an error |
| 5 | `intr_wstrb_err` | write strobes not fully set |
| 6 | `intr_spurious_resp` | unexpected write response |
| 7, 8 | `intr_spare_0/1` | functional spares |
| 9 … 63 | `sprot_intr_*` | FIS security-protection block |

RDM rolls up to the apex aggregator as a **single** summary NMI
(`rdm_nmi` / `rdm_summary`, node `RDM`).

---

## 3 · RDM placement `[HIGH · OBSERVED for the map; MED · INFERRED for the relationship]`

`INTC_RDM` is a `0x8000` (32 KiB) container under `APB_IO_*_USER_IO`:

```
INTC_RDM                base+0x81000  0x8000   (container)
  INTC_RDM_NOTIFIC      +0x81000  0x1000  → csrs/notific/notific_10_queue.json
  INTC_RDM_MSIX         +0x82000  0x1000  → csrs/intc/intc_1grp_msix_unit.json
  INTC_RDM_CTRL         +0x83000  0x1000  → csrs/rdm/rdm_model.json     ⟵ RDM
  INTC_RDM_MISC_RAM     +0x84000  0x1000  → csrs/misc/misc_ram_model.json
```

`rdm_model.json` maps at **4 absolute bases** in Cayman (2 dies × {IO, PEB-mirror}):

| node | absolute base |
|---|---|
| `APB_IO_0_…_INTC_RDM_CTRL` | `0x000008006C83000` |
| `APB_IO_1_…_INTC_RDM_CTRL` | `0x000808006C83000` (die-1 mirror, bit-47) |
| `PEB_APB_IO_0_…_INTC_RDM_CTRL` | `0x020008006C83000` |
| `PEB_APB_IO_1_…_INTC_RDM_CTRL` | `0x020808006C83000` |

RDM sits with a `notific_10_queue`, an MSI-X unit and a `misc_ram` — i.e. it is the
descriptor-ring write-back engine for **interrupt / notification** delivery, a
sibling of (not part of) the SDMA `udma_m2s` / `udma_s2m` channels. It is never
co-located with `ccl` / `reduce` / `collective`; "RDM" here is **Ring-Descriptor**,
not reduction- or remote-DMA. `[map HIGH · OBSERVED; sibling/negative-evidence framing MED · INFERRED]`

---

## 4 · TOP_SP — `top_sp_ram` config surface

### 4.1 · The container and where `top_sp_ram` sits

TOP_SP is a **4 MiB** NODE (maverick address-map view) / a **256 KiB** APB-IO view
(Cayman), holding three 1 MiB sub-windows + 1 MiB reserved:

```
TOP_SP  (NODE, 4 MiB)
  +0x000000  TPB_EVT_SEM  0x100000  → the global EVENT/SEMAPHORE tsync block (§5)
  +0x100000  RAM          0x100000  → hosts the top_sp_ram CSR at RAM+0x0
  +0x200000  TPB_SP       0x100000  → the SP sequencer: Xtensa-NX core
                                      (IRAM 0x8000 | NX_IRAM 0x20000 |
                                       NX_DRAM 0x20000 | LOCAL_REG 0x10000)
  +0x300000  reserved     0x100000
```

`top_sp_ram` is therefore the small per-instance **config/status surface** of the
embedded Xtensa-NX core — not the bulk state. The NX sequencer's own registers are
in `TPB_SP/LOCAL_REG` (`tpb_xt_local_reg`); the sync substrate is `TPB_EVT_SEM`
(§5); notifications flow through a co-located `notific_10_queue`.

> **v5 / MAVERICK caveat.** The maverick `TOP_SP_RAM.json` shipped in the tree is an
> **address-map NODE only** — a 1 MiB window whose single `Include` is a
> `reserved.json` of size `0x1000` (`Type: NODE`, not `REGFILE`). The maverick CSR
> **interior** (the actual register bitfields) is **not shipped**; only the window
> header is OBSERVED. All maverick-interior register claims are therefore INFERRED
> from the cayman/mariana family. `[header HIGH · OBSERVED; v5 interior INFERRED]`

### 4.2 · `top_sp_ram` — full register table (8 regs, 11 bitfields)

`top_sp_ram` has one bundle `top_sp_ram_cfg`; offsets are **hex** strings.

| off | reg | access | field (Position) | reset | description (JSON) |
|---|---|---|---|---|---|
| `0x0` | `cfg` | RW | `unused[31:0]` | 0 | unused config bits |
| `0x4` | `timestamp_inc` | RW | `value[23:0]` | **`0x400`** | increment value for the timestamp counters in **TPB SP and semaphore blocks** (the time-sync tick) |
| `0x8` | `top_sp_whoami` | RO | `value[8:0]` | **`0xff`** | BlockID of this TOP_SP (`top_sp0-9 ⇒ 0x98–0xa1`) |
| `0xc` | `top_sp_nx_spc_lo` | RO | `value[31:0]` | 0 | program counter of the NX core, lower 32 b |
| `0x10` | `sw_queue` | RW | `sp_nx_nt[3:0]` | 0 | SW-queue # for SP-sequencer NX-generated (implicit) notifications |
| | | | `sp_explicit_nt[7:4]` | 0 | SW-queue # for explicit notifications |
| | | | `events_semaphores_nt[11:8]` | 0 | SW-queue # for Event/Semaphore notifications |
| | | | `errors_nt[15:12]` | 0 | SW-queue # for SP error notifications |
| `0x14` | `top_sp_nx_spc_hi` | RO | `value[31:0]` | 0 | NX core PC, upper 32 b |
| `0x900` | `spare_reg0` | RW | `value[31:0]` | 0 | spare 0 |
| `0x904` | `spare_reg1` | RW | `value[31:0]` | **`0xffffffff`** | spare 1 |

Three control surfaces are exposed here, all referencing the **same** embedded NX
core: (a) **PC readback** — `top_sp_nx_spc_lo` / `_hi` give a 64-bit program counter
so the host can observe the running SP sequencer; (b) **notification routing** —
`sw_queue` maps four notification classes (implicit-NX, explicit, event/semaphore,
error) onto four 4-bit SW-queue selectors; (c) **the time-sync tick** —
`timestamp_inc` is the increment fed to the timestamp counters in TPB-SP **and the
semaphore blocks**, i.e. the §5 EVT_SEM array advances on this tick.

> **GOTCHA — `whoami` width / encoding diverges on sunda.** `top_sp_ram.json` is
> byte-identical cayman == mariana == mariana_plus, but **sunda differs (8 diff
> lines)**, all in `top_sp_whoami`:
> cayman/mariana = `value[8:0]`, *"top_sp0-9 ⇒ 0x98–0xa1"* (10 TOP_SP + 10 PEB_SP);
> sunda = `value[7:0]`, *"top_sp_0–5, peb_sp_0–1 (encoded 6 and 7)"* (6 TOP_SP +
> 2 PEB_SP). A control plane that decodes `whoami` must branch on generation.

### 4.3 · TOP_SP is collective-sequencer **engine 5** `[role HIGH · CARRIED; not present in these two JSONs]`

Neither `top_sp_ram.json` nor `rdm_model.json` contains an `engine` enumerator.
The **engine-5** identity is established in the committed siblings
[TOP_SP collective lowering](../../collectives/ops/top-sp-lowering.md) and
[`pkl` TOP_SP coverage](../address/pkl-topsp-coverage.md):
`ENGINE_TOP_SP = 5`. TOP_SP is the per-NeuronCore **on-device collective
sequencer** — once kicked, the Xtensa-NX core **walks a SPAD `cc_op` program** the
host loaded into its IRAM, issuing the **DMA tail-pointer increments** and
**`EVT_SEM` arrive/wait/dec** ops that sequence each collective step, drives the
`tsync` tick, gates barriers, and posts completion as leader.

The two control surfaces in this page are exactly the levers that role uses:
the §5 EVT_SEM array is the synchronization primitive the `cc_op` program drives;
`timestamp_inc` (§4.2) is the `tsync` tick that orders cross-engine steps; and the
**descriptor tail-pointer write-back** idiom of §2 is the host-memory completion
mechanism the sequencer's DMA-tail increments ultimately feed. (RDM itself is the
**INTC**-side instance of that idiom, distinct from the SDMA tail pointers the
sequencer touches — they share the model, not the block.) `[engine-5 HIGH · CARRIED]`

---

## 5 · Global EVENT / SEMAPHORE (`tsync`) — hosted by TOP_SP

`TPB_EVT_SEM` (1 MiB at TOP_SP `+0x0`) is the classic Trainium **event +
semaphore** primitive: distinct APB op-windows **alias the same semaphore array**,
each applying a different operation on access. Maverick `TPB_EVT_SEM.json`:

```
+0x0000  EVENT_             0x400
+0x0400  EVENT_RESERVED0    0xC00
+0x1000  SEMAPHORE_READ     0x400   read
+0x1400  SEMAPHORE_SET      0x400   set  (overwrite)
+0x1800  SEMAPHORE_INC      0x400   atomic increment
+0x1C00  SEMAPHORE_DEC      0x400   atomic decrement
+0x2000  SEMAPHORE_CNTR_INC 0x400   counter-increment   ⟵ maverick-only extra port
+0x6000  EVENT_RESERVED1    0xFA000
```

The committed sibling [EVT_SEM address regions](../address/evt-sem-regions.md)
pins the **TPB_0** container at `0x2802700000` with **256 events + 256
semaphores** and the four op windows at `read +0x1000 / set +0x1400 /
inc +0x1800 / dec +0x1C00` (each `op sem[N]` at `window + 4·N`). The **TOP_SP**
instances live elsewhere: Cayman `TOP_SP_0` base `0x8280000000`, stride
`0x40000000`, with `TOP_SP_0_TPB_EVT_SEM` = the first 1 MiB (`EVENT@+0x0`,
`SEMAPHORE_READ@+0x1000`, set/inc/dec at `+0x1400 / +0x1800 / +0x1C00`).

> **CORRECTION — keep `0x2802700000` attached to TPB, not TOP_SP.** The sibling's
> container base `0x2802700000` is the **TPB_0** EVT_SEM instance. The same op-window
> layout applies to the TOP_SP-hosted EVT_SEM, but **its** base is `0x8280000000`
> (TOP_SP_0). Cayman TOP_SP EVT_SEM has **no `SEMAPHORE_CNTR_INC` port** — that
> fifth window is a maverick-only extension. Don't carry the TPB base onto the
> TOP_SP block.

**Tie to device `tsync`.** `timestamp_inc` (§4.2) drives the timestamp counters in
"TPB SP and semaphore blocks"; an EVT_SEM semaphore overflow raises TOP_SP INTC
cause 0 (`err_sem_overflow`, §6). So TOP_SP both **hosts** the semaphore array and
**sources** the timestamp tick — the SoC-level time-sync anchor device firmware
programs for cross-engine ordering. The exact firmware programming sequence is not
in these artifacts; the hardware hosting is OBSERVED, the `tsync` tie is
`[MED · INFERRED]`.

---

## 6 · TOP_SP interrupt triggers (cayman, 82 causes)

`aws_intc_top_sp_count = 82` (cayman). The stable, register-correlatable causes:

| cause(s) | name | correlates to |
|---|---|---|
| 0 | `err_sem_overflow_triggers` | the EVT_SEM semaphore array (§5) — DIRECT |
| 1 / 2 | `top_sp_erg_uncorretable_error` / `_corretable_error` | the ERG ECC block protecting TOP_SP RAM |
| 3 … 13 | `fis_cntrl_intr[0..4]`, `fis_sprot_intr[0..5]` | FIS / security-fabric front-end (not in `top_sp_ram`) |
| 14 … 63 | `fis_errtrig_intr[0..49]` | user/amzn err-triggers: wr-buffer full/drop, SW-NQ full/disabled, AXI-werr, stall, threshold, overlap, coalescer |
| 64 … 72 | `top_sp_notific_intr[0..8]` | this instance's own `notific_10_queue` |
| 73–76 | `top_sp_notify_error_wr_buffer_full/drop_0/1` | back-pressure on the notification queues (the `sw_queue` selectors, §4.2) |
| 77 / 78 | `nx_non_fatal` / `nx_fatal` | the embedded NX core whose PC is read at `+0xc`/`+0x14` |
| 79 … 81 | `nx_interrupt_2/3/4` | NX `int_enable` / unused |

Cause count varies by generation: **cayman 82**, mariana / mariana_plus / maverick
**80**, sunda **76** — the delta is entirely in the FIS / SPROT / parity region
(causes ≥ 3). The **stable** set across all gens: `err_sem_overflow` (0), ERG UE/CE
(1/2), `notific_intr[0..8]`, `notify_error_wr_buffer_full/drop`, and
`nx_non_fatal / nx_fatal / nx_interrupt_2/3/4`.

---

## 7 · TOP_SP WOB / QoS misc CSRs (mariana family)

The NX-core AXI-master write-ordering and QoS controls are **not** in `top_sp_ram`;
they live in two small sibling CSRs present only on some generations:

**`top_sp_misc_amzn`** (bundle `wob` — mariana / mariana_plus / maverick-amzn):

| off | reg | field | role |
|---|---|---|---|
| `0x00` | `wob_wr_bypass` | `on[0]` | WOB write bypass |
| `0x04` | `wob_wr_clear` | `en[0]` | WOB write clear |
| `0x08` | `wob_force_inorder` | `en[0]` | force in-order writes |
| `0x0C` | `wob_use_wid_base` | `done[0]` | use WID base |
| `0x10` | `wob_wid_base` | `addr[4:0]` | WID base |

⇒ **WOB** = the NX-core AXI-master **Write-reOrder-Buffer** control (in-order force,
bypass, WID base).

**`top_sp_misc_user`** (bundle `qos` — mariana / mariana_plus only):

| off | reg | field | role |
|---|---|---|---|
| `0x00` | `nx_awuser_override` | `en[0]` | enable AW-USER override |
| `0x04` | `nx_aruser_override` | `en[0]` | enable AR-USER override |
| `0x08` | `nx_awuser` | `val[1:0]` | 2-bit AW QoS class |
| `0x0C` | `nx_aruser` | `val[1:0]` | 2-bit AR QoS class |

⇒ **QoS** = override the NX-core AXI `AxUSER` (aw/ar) bits with a 2-bit class.

Presence: mariana + mariana_plus have **both**; maverick has **wob only**;
**cayman + sunda have NEITHER** as a CSR JSON. A rebuild targeting Cayman must not
assume a programmable WOB/QoS surface here.

---

## 8 · Cross-gen summary

| aspect | cayman | mariana / mariana_plus | maverick (v5) | sunda |
|---|---|---|---|---|
| `rdm_model.json` | 236 reg / 24 q / 242 bf | byte-identical | header-only (`rdm.h`) | byte-identical |
| `aws_intc_rdm_count` | 64 | 64 | 64 | 64 |
| RDM leaf instances | 4 (2 dies × {IO, PEB-mirror}) | 4 | — | 4 |
| `top_sp_ram.json` | 8 reg / 11 bf | byte-identical | NODE header only | **differs** (`whoami`) |
| `top_sp_whoami` | `[8:0]`, 10 TOP_SP + 10 PEB_SP | same | (interior INFERRED) | `[7:0]`, 6 TOP_SP + 2 PEB_SP |
| EVT_SEM extra port | no `SEMAPHORE_CNTR_INC` | — | **has** `SEMAPHORE_CNTR_INC` | no |
| WOB / QoS misc CSR | **neither** | wob + qos | wob only | **neither** |
| `aws_intc_top_sp_count` | 82 | 80 | 80 | 76 |
| PEB→apex rollup | single `top_sp_combined_nmi` | — | — | per-instance `top_sp_{0..5}_nmi` |

TOP_SP instance multiplicity (cayman flat map): `TOP_SP_0..19` = 20/die × 2 dies;
`TOP_SP_0` base `0x8280000000`, stride `0x40000000`.

---

## 9 · Open items

* **"RDM" acronym** — *Ring Descriptor Manager* is INFERRED from register/trigger
  semantics; no shipped string spells it out. Mechanism HIGH; the name MED. It is
  definitively **not** reduction-/remote-DMA or a collective-reduce model (always
  under INTC, never co-located with `ccl` / `reduce`). `[MED · INFERRED]`
* **Per-queue field descriptions** — blank in `rdm_model.json`; semantics imported
  from the sibling `cayman/rdm.h` (identical widths/masks). Cross-gen field identity
  HIGH; treating header text as authoritative for the JSON's blank fields is MED.
* **`tsync` firmware sequence** — only the hardware hosting (semaphore array +
  timestamp tick) is OBSERVED; the exact NCFW programming of `timestamp_inc` and
  the EVT_SEM handshake is not in these artifacts. `[MED · INFERRED]`
* **maverick interior** — only the address-map window header is shipped; the v5 CSR
  bitfields are INFERRED from the cayman/mariana family.
