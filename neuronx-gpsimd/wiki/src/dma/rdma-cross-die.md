# RDMA Cross-Die SBUF→SBUF P2P

This page decodes the **complete cross-die remote-DMA path** — how a GPSIMD NeuronCore
streams State-Buffer bytes into a *peer* NeuronCore's State-Buffer when the peer sits on the
**other die of the same Cayman package** or a **different package in the multi-die mesh**.
It is the data-plane leg every collective (all-reduce / reduce-scatter / all-gather /
send-recv) is built from. Five concerns are reconstructed, each anchored to a shipped
artifact byte-read:

| | concern | deliverable | grounding |
|---|---|---|---|
| **A** | `remote_routing_id` → SoC-address **fold** | `{DIE, CAYMAN_ID, CAYMAN_ID_VALID}` / `{EXIT_DIE, NEIGHBOR_ROUTE, …}` SET-chain | §2 |
| **B** | the `io_d2d` die-to-die **transport** | DWC-PCIe + Marvell XSR SerDes + iATU (Cayman); native UCIe (Maverick) | §3 |
| **C** | TX=M2S / RX=S2M tail-pointer **handshake** | `TDRTP_inc` / `RDRTP_inc` doorbells + `left_pop`/`right_push` | §4 |
| **D** | the two-semaphore **completion** protocol | `local_sem` / `remote_sem` over the EVT_SEM `+0x1800` INC window | §5 |
| **E** | per-generation **orchestration** split | on-engine Q7 `0xBF`, NCFW-driven steps, Maverick UCIe re-IP | §6, §7 |

The two device primitives — `rdma_desc_gen` (ExtendedInst op **8**) and `rdma_desc_start`
(op **9**) — live in the **Q7 POOL** data-plane firmware (`remote_copy.cpp`) carved from
`libnrtucode_internal.so`; the SoC field codec is macro-proven from the shipped
`cayman_addr_decode{,_neighbor}.h`; the transport IP is JSON-proven from the Cayman
`csrs/d2d/*.json` and the Maverick `al_address_map_db.pkl`. This page is the cross-die
**transport** companion to the two firmware kernel pages — the
[SB2SB remote-copy collective](../firmware/kernels/sb2sb-remote-copy.md) (the `0xBF`
sequencer leg) and the
[RDMA descriptor gen/start primitives](../firmware/kernels/rdma-desc-gen-start.md) (the
byte-exact `gen`/`start` decode) — which it does not re-derive but **routes**.

Confidence/evidence tags follow the [confidence model](../reference/confidence-model.md):
`OBSERVED` = byte/string/instruction/struct read from a shipped artifact;
`INFERRED` = reasoned over OBSERVED facts; `CARRIED` = consolidated from a cited cross-page
anchor; crossed with `HIGH`/`MED`/`LOW`. Callouts: **QUIRK** (counter-intuitive but real),
**GOTCHA** (a reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE**
(orientation). **WALL:** every Maverick (v5) *interior* claim is `INFERRED` — its Q7 firmware
is **not** in the shipped runtime archive; only its address-map DB and ISA headers are
OBSERVED. The v5 D2D **PHY** is not named in the customop artifacts; it is INFERRED at IP
level from the `amzn_ucie_*` block names.

---

## 0. The path in one diagram

```
   THIS NeuronCore (die 0)                              PEER NeuronCore (die 1 / other pkg)
 ┌───────────────────────────┐                       ┌───────────────────────────┐
 │  SBUF  [src_addr,+free_dim]│                       │  SBUF  [dst_addr,+free_dim]│
 └────────────┬──────────────┘                       └──────────────▲────────────┘
              │ Q7 iDMA (M2S read)                                   │ Q7 iDMA (S2M write)
   ┌──────────▼───────────────────────────┐          ┌──────────────┴───────────────────┐
   │ rdma_desc_gen (op8) @0x161f4          │          │ rdma_desc_gen (op8) on the peer   │
   │  • FOLD dst → 57-bit routed SoC addr  │          │  • program_window (remap remote   │
   │    {DIE,CAYMAN_ID,VALID} | {EXIT_DIE, │          │    SBUF into local Q7 window)     │
   │     NEIGHBOR_ROUTE,ID_VALID}   (§2)   │          │  • build CME-COPY ring +          │
   │  • build CME-COPY BD ring + LOCAL +   │          │    LOCAL/REMOTE sema BDs          │
   │    REMOTE sema descriptors      (§5)  │          └───────────────────────────────────┘
   └──────────┬────────────────────────────┘
              │ rdma_desc_start (op9) @0x1723c — role-split by PRID parity (§4)
   ┌──────────▼───────────────┐                       ┌───────────────────────────┐
   │ TX leg (a6≠0):           │   right_push  ◀───────│ RX leg (a6==0):           │
   │  left_pop WAIT RX signal │                       │  s32i.n a3,a4,0 →          │
   │  s32i.n a3,a4,0 →         │   ────────────▶       │  S2M_Q.RDRTP_inc (arm rx) │
   │  M2S_Q.TDRTP_inc (LAUNCH) │   data over io_d2d    │  right_push SIGNAL TX     │
   └──────────────────────────┘   (DWC-PCIe+XSR /UCIe) └───────────────────────────┘
              │                                                  │
              ▼ EVT_SEM.inc(local_sem) +0x1800   (source release)
                                              EVT_SEM.inc(remote_sem) +0x1800 ◀ routed over io_d2d
                                                   (data-ready at peer; next step waits ≥ target)
```

One-line verdict: **a cross-die SBUF→SBUF transfer is `gen`(fold the dst into a routed SoC
address + build the ring + two sema BDs) → `start`(PRID role-split → RX arms S2M tail +
`right_push`; TX `left_pop`-waits then rings M2S tail = launch), the bytes and the routed
remote-sema increment riding the io_d2d data fabric, completion signalled by the
local/remote semaphore pair chained step-to-step by the NCFW counted barrier.**

---

## 1. The two primitives + the carve

Carve container: `…/custom_op/c10/lib/libnrtucode_internal.so`. The Q7 POOL DEBUG images are
`.rodata`-resident `_get.data` symbols, so **file offset == device VA** (`.rodata`
`Address==Off==0x46b0`; no `.data` delta). All addresses below are Q7 IRAM file offsets ==
device VAs.

| object | `nm` symbol | file off / size | sha256[:12] | role |
|---|---|---|---|---|
| `CAYMAN_Q7_POOL_DEBUG_IRAM` | `…_get.data` | `0x249020` / `0x1ea40` (125 504 B) | `513a8a22d94b` | both fn bodies + TX/RX block |
| `CAYMAN_Q7_POOL_DEBUG_DRAM` | `…_get.data` | `0x267a60` / `0x15d00` (89 344 B) | `226f4254d475` | the log strings + role/queue tokens |
| `CAYMAN_NX_POOL_DEBUG_IRAM` | `…_get.data` | `0x1b1420` / `0x1c820` (116 768 B) | (SEQ `0xBF` handler) | sequencer anchor `@0xD1E4` |

**[HIGH / OBSERVED — `nm -S` + `sha256sum`; shas reproduce the FW-29/FW-30
anchors exactly; reset vector `06 7f 00 00` = `j 0x1fc`.]**

The two ExtendedInst primitives (sub-opcodes **8** / **9** of the Q7 ExtendedInst space,
`EXTENDED_RDMA_DESC_GEN=8` / `EXTENDED_RDMA_DESC_START=9`, read verbatim from
`neuron_cayman_arch_isa/tpb/aws_neuron_isa_tpb_extended_utils.h`):

| | `rdma_desc_gen` (op 8) | `rdma_desc_start` (op 9) |
|---|---|---|
| entry (Q7 IRAM) | `0x161f4` `entry a1,0x2540` (9.5 KiB frame) | `0x1723c` `entry a1,0x100` (256 B) |
| job | run SWDGE → build per-engine CME BD ring + LOCAL+REMOTE sema BDs + **fold the dst** | drain → PRID role-split → ring the tail-pointer doorbell |
| operands | `{local_sem@14, remote_sem@15, remote_core_id@16, remote_routing_id@20, dma_engine_mask@24, src_addr@28, dst_addr@32, free_dim_bytes@36, is_bidirectional@40, …}` | **none of its own** — consumes the ring `gen` already built |
| critical path | **off** — "RdmaDescGen can happen earlier without waiting" (header) | **on** — carries the `events` wait/update that gates the launch |

**[HIGH / OBSERVED — entry bytes `36 81 4a` @`0x161f4` (= `entry a1,0x2540`) and `36 01 02`
@`0x1723c` (= `entry a1,0x100`) read; the struct field offsets and the
`EXTENDED_RDMA_DESC_GEN=8`/`…START=9` enum byte-read from the ISA header. The full byte-exact
decode of both primitives is on the [gen/start page](../firmware/kernels/rdma-desc-gen-start.md);
this page consumes that decode and routes it.]**

> **NOTE — `gen` and `start` are reusable P2P primitives, not phases of `0xBF`.** They are
> *called* by the SB2SB(op-6) driver — the call edge `0x3742 → 0x161f4` (bytes `25 ab 12` =
> `call8 0x161f4`, decoded) — but they are *also* their own ExtendedInst ops
> with their own 64-byte words. A reimplementer exposes all three: `0xBF` is the collective,
> `gen`/`start` are the transport any kernel can issue directly. **[HIGH / OBSERVED.]**

---

## 2. Deliverable A — `remote_routing_id` → SoC-address FOLD (field-exact)

The SoC byte address each descriptor `buf_ptr` carries is decoded/encoded by the shipped
`cayman_addr_decode{,_neighbor}.h` GET/SET macros, **read verbatim and byte-identical** this
session from `extracted/nested/cayman-arch-regs_tgz/output/address_map/`. The two decoders
share `#include` guard `CAYMAN_ADDR_DECODE_H` — they are a **drop-in alternate view**
(mutually exclusive in one TU), selected by whether the peer is reachable by chip-id or only
by neighbor-route.

### 2.1 LOCAL decoder — the chip-id routed view (`cayman_addr_decode.h`)

| bit range | field | mask/shift (macro-exact) | meaning |
|---|---|---|---|
| `[46:0]` | `LOCAL` | `(a>>0)&0x7fffffffffff` | per-die byte addr (SBUF byte in STATE_BUF aperture) |
| `[47]` | `DIE` | `(a>>47)&0x1` | which die of the 2-die package |
| `[53:48]` | `CAYMAN_ID` | `(a>>48)&0x3f` | chip id in the mesh (2⁶ = 64 dies) |
| `[54]` | `CAYMAN_ID_VALID` | `(a>>54)&0x1` | `=1` ⇒ route by chip id; `=0` ⇒ stay on chip |
| `[55]` | `RESERVED` | `(a>>55)&0x1` | — |
| `[56]` | `PCIE_ATTR_RELAXED_ORDERING` | `(a>>56)&0x1` | PCIe TLP relaxed-ordering hint |
| `[57]` | `OK_TO_FAIL` | `(a>>57)&0x1` | poison / ok-to-fail |

`SET_F(a,v) = (a & ~(m<<s)) | ((v&m)<<s)` — clear-then-insert; GET/SET are
symmetric/lossless per field. **[HIGH / OBSERVED — all 8 macro pairs read verbatim.]**

### 2.2 NEIGHBOR decoder — the one-/two-hop routed view (`cayman_addr_decode_neighbor.h`)

Re-purposes `[53:48]` (where `CAYMAN_ID` sat) as a routing-path overlay:

| bit range | field | mask/shift (macro-exact) | meaning |
|---|---|---|---|
| `[46:0]` | `LOCAL` | `(a>>0)&0x7fffffffffff` | (unchanged) |
| `[47]` | `DIE` | `(a>>47)&0x1` | (unchanged) |
| `[49:48]` | `NEIGHBOR_RSVD` | `(a>>48)&0x3` | reserved |
| `[50]` | `EXIT_SENG` | `(a>>50)&0x1` | leave the SEngine domain |
| `[51]` | `EXIT_DIE` | `(a>>51)&0x1` | cross the die boundary |
| `[52]` | `NEIGHBOR_ROUTE` | `(a>>52)&0x1` | engage neighbor routing |
| `[53]` | `PEB` | `(a>>53)&0x1` | target PEB / PEB routing |
| `[54]` | `ID_VALID` | `(a>>54)&0x1` | routing-id valid (**same bit** as `CAYMAN_ID_VALID`) |
| `[55..57]` | `PCIE_U_RSVD` / `PCIE_ATTR_RELAXED_ORDERING` / `OK_TO_FAIL` | (unchanged) | |

**[HIGH / OBSERVED — all 11 macro pairs read verbatim; the `EXIT_*`/`ROUTE`/`PEB`
SEMANTICS are named-only (no behavioural prose in the header) — MED.]**

> **GOTCHA — bit 54 is shared.** `CAYMAN_ID_VALID` (local view) and `ID_VALID` (neighbor view)
> are the *same* physical bit `[54]`. The two decoders never coexist in one address; the high
> nibble means "chip id" or "neighbor route" depending on which decoder the firmware compiled
> against. A reimplementer must not treat `[53:48]` as both a 6-bit `CAYMAN_ID` and a packed
> `{EXIT_SENG,EXIT_DIE,NEIGHBOR_ROUTE,PEB}` simultaneously. **[HIGH / OBSERVED.]**

### 2.3 The fold (what `rdma_desc_gen` does with `remote_routing_id`)

`remote_routing_id` (struct off **20**, sourced "from GpSimd register" per the header) is the
**routing** id of the peer core (distinct from `remote_core_id`@16, the *physical* id).
`dst_addr` (off 32) starts as the peer's LOCAL SBUF partition offset `[46:0]`; the fold writes
the routing id into the high bits in one of two modes:

```c
// rdma_desc_gen FOLD — turn the peer's LOCAL dst offset into a routed 57-bit SoC address.
// dst_local = ADDR4-decoded peer SBUF partition offset (bits [46:0]).
// Mode selected by the §2.4 device-side is_die_0/engine_idx classifier + host topology (§7).

uint64_t fold_chip_id(uint64_t dst_local, uint8_t peer_die, uint8_t peer_chip_id) {
    uint64_t s = 0;
    s = CAYMAN_ADDR_DECODE_SET_LOCAL(s, dst_local);             // [46:0]
    s = CAYMAN_ADDR_DECODE_SET_DIE(s, peer_die);               // [47]
    s = CAYMAN_ADDR_DECODE_SET_CAYMAN_ID(s, peer_chip_id);     // [53:48] <- routing_id
    s = CAYMAN_ADDR_DECODE_SET_CAYMAN_ID_VALID(s, 1);         // [54] = 1 → route remote
    return s;                                                  // different package / one-hop chip
}

uint64_t fold_neighbor(uint64_t dst_local, uint8_t peer_die) {
    uint64_t s = 0;
    s = CAYMAN_ADDR_DECODE_SET_LOCAL(s, dst_local);            // [46:0]
    s = CAYMAN_ADDR_DECODE_SET_DIE(s, peer_die);              // [47]
    s = CAYMAN_ADDR_DECODE_SET_EXIT_DIE(s, 1);               // [51] cross the die boundary
    s = CAYMAN_ADDR_DECODE_SET_NEIGHBOR_ROUTE(s, 1);         // [52] engage neighbor routing
    s = CAYMAN_ADDR_DECODE_SET_ID_VALID(s, 1);               // [54] routing-id valid
    return s;                                                 // other die / not-one-hop on-chip peer
}
```

The firmware's DEBUG log
`rdma_desc_gen [%s] after remote_routing_id=$%d, dst_addr=0x%llx, n_active_dmas=%d`
(DRAM `0x497b`, device VA `0x8497b`, OBSERVED byte-exact) prints `dst_addr` **immediately
after** this rewrite; the earlier `src_addr=0x%llx, dst_addr=0x%llx` (DRAM `0x493d`) prints
the **pre-fold** pair. The "$" before `%d` is literal in the format string — `remote_routing_id`
is logged as a decimal.

**[SoC bitfield HIGH / OBSERVED (macros); the routing_id→high-bit SET-chain is INFERRED-HIGH
from the two log positions (pre-fold `0x493d` → post-fold `0x497b`) + the symmetric SET macros;
the exact branch (chip-id vs neighbor) that fires per peer is host-topology-decided (§7) — MED.]**

### 2.4 The device-side die-decode — `is_die_0` / `engine_idx`

`remote_copy.cpp` carries the runtime inverse of §2.1's GET macros, a helper that takes the
peer's `engine_base_addr` + `tpb_base_addr` and classifies the peer, logged byte-exact
(DRAM `0x0f98`, OBSERVED):

```
P%i: engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u engine_idx=%u
```

`is_die_0` = the `DIE` bit `[47]` of the peer base; `engine_idx` = which engine within the
peer TPB; `is_tpb` = whether the base targets a TPB SBUF aperture. This is how the Q7 ucode
classifies a peer **local-die vs cross-die** (and selects the §2.3 fold mode) before building
the descriptor. **[the log string + its field set HIGH / OBSERVED; the bit-extract body is
FLIX-bundled in this image (the `0x0f98` const16 low-half hides in a VLIW bundle) — MED,
pinned by the named log fields + the §2.1 macro positions.]**

### 2.5 The remote-SBUF → local-Q7-window remap (`program_window`)

The Q7 cores address SBUF through a **32-bit NX-local window**, so a REMOTE SBUF SoC address
must first be programmed into a remapper window before the Q7 iDMA can reach it.
`remote_copy.cpp` logs this byte-exact (DRAM `0x0ffd` / `0x1856`, OBSERVED):

```
R: program_window: num=%d, vld=%d, xt_addr=0x%llx, soc_addr=0x%llx, u_mask=0x%llx, l_mask=0x%llx
R: program_window: num=%d, mask=0x%llx, match=0x%llx, replace=0x%llx
R: update_window:  num=%d, xt_addr=0x%llx, soc_addr=0x%llx, u_mask=0x%llx, l_mask=0x%llx
```

Each window maps a local `xt_addr` (the Q7-visible 32-bit address) to a `soc_addr` (the full
57-bit routed SoC address from §2.3), gated by `u_mask`/`l_mask` and a `match`/`replace` rule.
The `xt_addrs[16]` table the `gen` frame builds (log DRAM `0x4afa`,
`xt_addrs[16] = [0x%08x%08x, …×16]`) holds the **16 per-engine** Q7-window views of the peer's
SBUF — one per DMA engine in the `dma_engine_mask`. The pre-sync log
`remote_pool_xt_addr=0x%x, remote_q7_xt_addr=0x%x, sb2sb_ready_to_receive_remote=0x%x` (DRAM
`0x0c3e`) prints the two programmed window views + the remote ready flag.

**[program_window/update_window logs + the `xt_addrs[16]` table HIGH / OBSERVED; the remapper
CSR identity (`soc2xt_addr` / `xt_window.hpp` / `soc_window_manager.hpp` anchors, OBSERVED at
DRAM `0xbc4`/`0xbd0`/`0xf81`) MED; absolute window CSR offsets are not literal in this image — LOW.]**

---

## 3. Deliverable B — the `io_d2d` transport (the die-to-die fabric)

### 3.1 What the link is (Cayman / Mariana — CSR JSON-proven)

The `io_d2d` link is a **hybrid IP**: a Synopsys DesignWare PCIe protocol **controller** over
a **Marvell XSR** die-to-die SerDes PHY, with a Marvell **MPCS x16** PIPE coding layer
(ULFEC = ultra-low-latency FEC). The CSR units (`extracted/nested/cayman-arch-regs_tgz/csrs/d2d/`):

| unit (JSON) | what it is | OBSERVED proof |
|---|---|---|
| `snps_ctrl` | **DWC-PCIe controller** repurposed as the die-to-die link controller | PCIe cap suite present: `PF0_PCIE_CAP`, `PF0_AER_CAP`, `RAS_DES`, `PF0_PORT_LOGIC`, `PF0_PTM_CAP`, `PF0_ATU_CAP`, `LTSSM_VARIABLE` |
| `d2d_ctrl_axi_cfg` | the Amazon AXI-domain wrapper — **the source of the cross-die AXI transactions the SDMA descriptors hit** | **269** registers (`rg -c '"Name":'`) |
| `d2d_ctrl_core_cfg` | the core-domain wrapper | present |
| `mrvl_mpcs_x16` + `d2d_mpcs_cfg` | the PIPE coding + ULFEC FEC codeword stats | `ULFEC`/`FEC` strings (33 hits in `mrvl_mpcs_x16`) |
| `mrvl_xsr_phy` + `mrvl_xsr_pram` + `d2d_xsr_cfg` | the Marvell XSR die-to-die SerDes PHY | `XSR` (57 hits) |

**[HIGH / OBSERVED — the PCIe cap structures + the Marvell XSR/MPCS units prove a
DWC-PCIe-derived controller over a Marvell SerDes PHY; counts via `rg -c`.]**

### 3.2 The physical routing mechanism — iATU OUTBOUND

`PF0_ATU_CAP` (within `snps_ctrl`) carries the iATU OUTBOUND address-translation regions:
`IATU_REGION_CTRL_1/2_OFF_OUTBOUND_n` (**120** `IATU_REGION_CTRL` hits) +
`IATU_LWR/UPPER/LIMIT_BASE_ADDR_OFF_OUTBOUND_n` (**220** `OUTBOUND` hits total). These remap a
local AXI address to a remote-die PCIe address — **this is the hardware that implements the §2.2
neighbor routing**: setting `EXIT_DIE`/`NEIGHBOR_ROUTE` selects the iATU OUTBOUND region that
forwards the transaction across the link. `PF0_PTM_CAP` (Precision Time Measurement) keeps the
two dies' clocks aligned.

**[HIGH / OBSERVED — the iATU OUTBOUND register names + counts read from `snps_ctrl.json`; that
the iATU region ↔ neighbor-decoder binding is the routing plane — MED (no register binds the
two; the correspondence is reasoned from the field semantics).]**

### 3.3 Topology — D2D subsystems / mesh

In the **Cayman customop CSR view**, `address_map.yaml` instantiates **4** `snps_ctrl` + **4**
`mrvl_xsr_phy` blocks (`rg -c 'name: snps_ctrl'` = 4). The 6-bit `CAYMAN_ID` `[53:48]` → 2⁶ =
**64 dies** in the mesh; the host topology exposes **4 C2C ports per MLA** to the collective
router (§7.2) — matching the 4 D2D controller instances in this CSR view. All D2D blocks live
under `PEB_APB_IO` (176 `PEB_APB_IO` hits in `address_map.yaml`), and the bit-`[47]` `DIE`
mirror doubles them across the two dies.

**[HIGH / OBSERVED — the 4 snps_ctrl/4 mrvl_xsr_phy instance counts + the `PEB_APB_IO` anchor
read; the 64-die `CAYMAN_ID` mesh follows from the 6-bit field width.]**

> **NOTE — "8 subsys/die" vs "4 controllers".** A broader SoC-level enumeration counts 8 D2D
> *subsystems* per die (each with 2 controllers); the customop-shipped Cayman CSR view exposes
> the **4** `snps_ctrl` controller instances the GPSIMD reaches. The collective router's
> **4-C2C-ports-per-MLA** (§7.2) corresponds to this 4-controller view. **[the 4-instance count
> HIGH/OBSERVED; the subsystem-vs-controller reconciliation MED.]**

### 3.4 Data path ≠ interrupt path (the keystone)

The `io_d2d` INTC trigger set (`intc/d2d_triggers.yaml`, **216** triggers via `rg -c 'name:'`)
carries **only** link/PHY/error/fault sources (`fis_cntrl_intr_*`, `fis_errtrig_intr_*`). It
carries **ZERO** doorbell / tail-pointer / semaphore / collective triggers
(`rg -ci 'tdrtp|rdrtp|tail_ptr|doorbell|semaphore|sb2sb|collective|m2s|s2m'` = **0**).

> **GOTCHA — the collective DATA bytes and the two completion semaphores ride the io_d2d
> DATA path, NOT the INTC.** The bytes flow as `d2d_ctrl_axi_cfg` AXI transactions over the
> SerDes; the routed remote-sema increment is just another AXI write to the peer's EVT_SEM
> aperture (§5). The d2d INTC is for *link health*, not data-plane signalling. The header text
> (op-9, verbatim): *"Data traverses PCIE/RMVT/D2D links if cores are not HBM neighbors; uses
> on-chip routing if cores are not one-hop from each other."* So PCIE/RMVT/D2D are the three
> transport classes and `io_d2d` is the die-to-die one. **[HIGH / OBSERVED — the 216-trigger
> taxonomy carries zero data-plane triggers; header prose byte-read from the ISA header.]**

See the [CSR — HBM / D2D / PCIe blocks](../control/csr/hbm-d2d-pcie-blocks.md) page for the full
register-level decode of `snps_ctrl` / `d2d_ctrl_axi_cfg` / `mrvl_xsr_phy`.

---

## 4. Deliverable C — TX=M2S / RX=S2M handshake registers (byte-exact)

A full-duplex SDMA channel = one UDMA **M2S** (outbound, reads local) + one UDMA **S2M**
(inbound, writes remote), each 16 queues, each with a software descriptor ring; the
tail-pointer-increment register at group-offset **`+0x038`** is the **doorbell** ("write N to
advance the tail by N descriptors").

| | register | within-group off | absolute (queue 0) | access | `val` | from CSR JSON |
|---|---|---|---|---|---|---|
| **TX → M2S** | `TDRTP_inc` | `0x038` | `0x1038` | RW | `val[23:0]` "in descriptors" | `csrs/sdma/udma_m2s.json` |
| **RX → S2M** | `RDRTP_inc` | `0x038` | `0x1038` | WO | `val[23:0]` "in descriptors" | `csrs/sdma/udma_s2m.json` |

Both queue groups are `M2S_Q` / `S2M_Q` at group base `0x01000`, `BundleSizeInBytes 4096`,
**`ArraySize 16`** — 16 M2S queues mirrored by 16 S2M queues; the per-queue doorbell for queue
*q* is `0x1000 + q*0x1000 + 0x038`. **[HIGH / OBSERVED — CSR register names, the `0x038`
within-group offset, the 24-bit `val` width, the 16-queue array — see the
[gen/start page §4.7](../firmware/kernels/rdma-desc-gen-start.md).]**

### 4.1 Role → queue binding (the DRAM token block)

`rdma_desc_start` determines this core's role by a PRID-derived parity bit, then prints a `[%s]`
role token and, for the trigger, a `%s` queue token. The DRAM token layout (`xxd`):

```
0x487f "TX\0RX\0"     (TX@0x487f, RX@0x4882)   ← the [%s] role token
0x4d8c "M2S\0S2M\0"   (M2S@0x4d8c, S2M@0x4d90) ← the %s queue token for "Trigger %s DMA"
0x4e17 "remote_copy.cpp\0"                     ← the source-file tag (error path)
```

The adjacency `…cpu_id=%d\0 TX\0 RX\0` and `…n_desc=%d\0 M2S\0 S2M\0` PINS, byte-exact, that
**role TX prints "M2S"** (drives the M2S / `TDRTP_inc` queue) and **role RX prints "S2M"**
(drives the S2M / `RDRTP_inc` queue) — no inference needed, the binding is the NUL-terminated
token adjacency. **[HIGH / OBSERVED — token positions read via `xxd`.]**

### 4.2 The role split + doorbell stores (byte-exact, native `ncore2gp` objdump)

With `a3` = `num_descriptors` (the tail-inc COUNT), `a4` = the per-role doorbell MMIO address,
`a6` = the role flag (`0`=RX, `≠0`=TX), disassembled:

```
0x1736b  7807    l32i.n a7, a7, 0     ; a7 = the role/parity word
0x1736d  b0eb03  rsr.prid a11         ; a11 = this core's PRID
0x17370  707004  extui  a7, a7, 0, 1  ; a7 = bit0 of role word = PRID parity
0x17373  679775  bne    a7, a6, 0x173ec ; parity != role → ERROR path
0x17376  ac66    beqz.n a6, 0x173a0   ; *** a6==0 → RX path ; a6!=0 → TX path ***
--- TX path (sender), falls through @0x17378 ---
0x1737b  const16 a10,0x4e27 ; callx8 a5  ; LOG "[TX] Waiting for RX sync (left_pop)"
0x1738f  const16 a10,0x4e65 ; callx8 a5  ; LOG "[TX] Writing tail pointer increment"
0x1739b  3904    s32i.n a3, a4, 0     ; *** M2S_Q.TDRTP_inc <- num_descriptors == LAUNCH ***
0x1739d  j 0x173c9                     ; → common END
--- RX path (receiver), @0x173a0 ---
0x173a3  const16 a10,0x4ee1 ; callx8 a5  ; LOG "[RX] Writing tail pointer increment"
0x173a9  3904    s32i.n a3, a4, 0     ; *** S2M_Q.RDRTP_inc <- num_descriptors (arm rx buffers) ***
0x173b1  const16 a10,0x4f1f ; callx8 a5  ; LOG "[RX] Signaling TX to proceed (right_push)"
0x173c3  l32r   a12, …                  ; a12 = the right_push signal target (TX go-sema)
--- common END @0x173c9 ---
0x173ea  1df0    retw.n
```

Both tail-inc stores are the **identical** `s32i.n a3,a4,0` (bytes `39 04`) — the **DmaTrigger**
primitive — differing ONLY in the `a4` doorbell value (M2S `TDRTP_inc` for TX, S2M `RDRTP_inc`
for RX). **[HIGH / OBSERVED — the `extui`/`bne`/`beqz.n` role logic + both `39 04` stores +
the `[TX]`/`[RX]` log sequence + `retw.n` disassembled with native
`xtensa-elf-objdump XTENSA_CORE=ncore2gp`; the `bne`→`0x173ec` and `beqz.n`→`0x173a0` targets
decode exactly. The absolute `a4` doorbell VALUE is a negative PC-relative literal (image loads
at a non-zero IRAM base) so it is bound by §4.4's NCFW `dma_apb_bcast` SoC addrs — MED.]**

### 4.3 The `left_pop` / `right_push` producer-consumer handshake

The two legs are the two ends of one SDMA descriptor ring:

```c
// rdma_desc_start handshake — ring built by rdma_desc_gen.
// a3=num_descriptors; a6=role (0==RX, !=0==TX); a4=per-role doorbell.
void rdma_desc_start(dma_ctx_t *ctx, uint32_t num_descriptors, int role) {
    descriptor_stream_drain(ctx);                  // 0x17354/0x17365: flush BDs into ring
    uint32_t role_word = *ctx->role_ptr;           // 0x1736b
    if ((role_word & 1) != role)                   // 0x17373 bne -> 0x173ec
        FATAL("DescriptorStream wrote %d, expected %d");

    if (role != 0) {                               // 0x17376 beqz.n -> RX ; else TX
        // TX (local source)
        wait_for_rx_signal(ctx);                   // "left_pop": WAIT the RX go-sema
        *(volatile uint32_t *)ctx->m2s_inc_reg     // 0x1739b s32i.n a3,a4,0
            = num_descriptors;                     //   M2S_Q.TDRTP_inc += N == CME COPY LAUNCH
    } else {
        // RX (remote sink)
        *(volatile uint32_t *)ctx->s2m_inc_reg     // 0x173a9 s32i.n a3,a4,0
            = num_descriptors;                     //   S2M_Q.RDRTP_inc += N (publish empty rx bufs)
        signal_tx_proceed(ctx);                    // "right_push": SIGNAL the TX go-sema
    }
}
```

> **QUIRK — RX arms first, TX fires second.** RX is the *producer of empty descriptors*: it
> bumps its S2M tail to arm the receive ring, **then** `right_push` signals the sender. TX is
> the *consumer of that readiness*: it `left_pop`-**waits** for the RX signal (the
> `[TX] Waiting for RX sync (left_pop)` log precedes the store), **then** bumps its M2S tail —
> the actual CME COPY launch. Ordering RX-before-TX is what prevents the sender from writing into
> a receive aperture that isn't yet armed. **[handshake structure HIGH / OBSERVED (store order +
> logs); the `left_pop`=wait / `right_push`=signal DIRECTION INFERRED from the log wording + ring
> semantics — MED.]**

### 4.4 Where the doorbell SoC addresses come from (`dma_apb_bcast`)

The `a4` doorbell targets are NOT literal in the Q7 image; they are supplied by the NCFW
ring-channel config, which carries per channel:

```c
struct dma_apb_bcast {
    uint64_t m2s_tail_ptr;   // the TX M2S tail-inc doorbell SoC addr
    uint64_t s2m_tail_ptr;   // the RX S2M tail-inc doorbell SoC addr
    uint32_t mask;           // the broadcast queue-group mask
};
```

A single tail-inc write to the **broadcast** doorbell (`sdma_bcast_base`; `start` logs
`Trigger DMA; sdma_bcast_base = 0x%08x, trigger addr = 0x%08x, mask = 0x%08x n_desc=%d`, DRAM
`0x47f4`, OBSERVED) advances the tails of the **group** of queues `mask` selects,
launching the multi-engine move with one trigger. **[HIGH / OBSERVED — the bcast log this image
(DRAM `0x47f4`); the `dma_apb_bcast` struct CARRIED from the NCFW ring-channel config; the exact
bcast-group cut MED.]** The NCFW supplier is the
[NCFW DMA reprogram + APB broadcast](../collectives/ncfw/dma-reprogram-apb-bcast.md) page.

---

## 5. Deliverable D — the two-semaphore completion protocol

Completion is a **two-semaphore** model over the EVT_SEM atomic-increment window, plus a 16-B
SDMA BD generation-tag busy-poll. The two semaphores are named in the RDMA_DESC_GEN word
(`local_sem`@14, `remote_sem`@15) and bumped by two extra semaphore-increment BDs that
`rdma_desc_gen` pushes into each engine's ring (gen logs OBSERVED):

```
0x4bd6  P%i: Q7: rdma_desc_gen [%s] Pushing local  semaphore descriptor, tpb_idx=%d, sem=%d
0x4c2a  P%i: Q7: rdma_desc_gen [%s] Pushing remote semaphore descriptor, remote_tpb=%d, routing_id=%d, sem=%d
```

> **NOTE — the remote sema BD carries `routing_id`.** That is the **same cross-die fold** (§2.3):
> the remote-sema increment is routed to the PEER's EVT_SEM aperture over `io_d2d`, exactly like
> the data bytes — it is *not* an interrupt. **[HIGH / OBSERVED — the `routing_id` field on the
> remote-BD log string.]**

### 5.1 The EVT_SEM engine (the increment target)

`TPB_*_EVT_SEM` exposes per instance **256** hardware semaphores through four 4-B-stride
operation windows (`csrs/tpb/tpb_events_semaphores_axi.json`, read byte-exact):

| window | `AddressOffset` | access | role |
|---|---|---|---|
| `tpb_semaphores_read` | `0x1000` | RO | read value |
| `tpb_semaphores_set` | `0x1400` | WO | overwrite |
| `tpb_semaphores_inc` | **`0x1800`** | WO | **atomic +=** ← the sema-increment BD writes HERE |
| `tpb_semaphores_dec` | `0x1C00` | WO | atomic -= |

(`ArraySize 256`, `BundleSizeInBytes 0x4` confirmed.) The LOCAL sema BD writes the
LOCAL core's `EVT_SEM +0x1800[local_sem*4]`; the REMOTE sema BD writes the PEER core's
`EVT_SEM +0x1800[remote_sem*4]`, the peer aperture carrying the §2.3 `CAYMAN_ID`/`EXIT_DIE` high
bits. The firmware materializes the `+0x1800` window in the gen-helper region
(`const16 a7,0x1800` @`0x16ef6`, bytes `74 00 18`, OBSERVED). **[the EVT_SEM window layout +
the `+0x1800` materialization HIGH / OBSERVED; the per-sema target INFERRED-HIGH from the BD
semantics + the `routing_id` field on the remote BD.]**

### 5.2 The increment / wait sequence (per ring/mesh step)

```c
// One cross-die ring/mesh STEP — the two-semaphore lifecycle.
rdma_desc_gen(/*op8*/);          // (1) build ring across all 128 partitions + 2 sema BDs
rdma_desc_start(/*op9*/);        // (2) drain, PRID role-split, doorbell M2S(TX)/S2M(RX)
// (3) LOCAL completion (source release): when the local DMA engine finishes triggering,
//     the LOCAL sema BD does EVT_SEM.inc(local_sem) on THIS core
//     -> "the source memory can be released" — bumped by all 16 DMA engines.
// (4) REMOTE completion (data-ready): when ALL bytes land at the peer, the REMOTE sema BD —
//     routed by routing_id over io_d2d — does EVT_SEM.inc(remote_sem) on the PEER
//     -> "notifies remote engines dst_buffer is ready to read" — bumped by # of DMA engines.
// (5) the NEXT step's rdma_desc_start on the peer WAITS recv_sema >= target then atomic-decrements,
//     via the NCFW counted barrier (EVT_SEM TPB op subop 20 = add_semaphore_wait_ge_and_dec,
//     subop 21 = add_semaphore_inc) — chaining the legs into a ring/mesh traversal.
```

The header semantics (op-8, verbatim): *"local_sem: Incremented by all 16 DMA engines when local
gpsimd finishes triggering DMA, releases handle on local buffer so it can be written into again;
remote_sem: Incremented by # of DMA engines when all bytes arrive at remote core's data buffer,
notifies remote engines dst_buffer is ready to read."* **[the two-semaphore semantics HIGH /
OBSERVED (header + gen logs); the EVT_SEM window binding HIGH / OBSERVED (JSON); the
`add_semaphore_inc`/`wait_ge_and_dec` op binding CARRIED from the EVT_SEM/NCFW barrier anchors;
the per-engine inc count CARRIED.]**

### 5.3 The generation-tag poll + the structural guard

Underneath the semaphores, each 16-B SDMA completion BD carries a 2-bit generation tag,
busy-polled until it matches the expected generation before a ring slot is reused. There is **no
interrupt** for the collective data plane (§3.4). Independently, `rdma_desc_start`'s role/parity
`bne @0x17373 → 0x173ec` + `ERROR: DescriptorStream wrote %d descriptors, expected %d` (DRAM
`0x5040`, OBSERVED) is a **HARD error** if the SWDGE produced a count ≠ expected — a structural
completion check independent of the semaphores. **[the gen-tag format CARRIED (DWARF-decoded
SDMA_CME_BD_DESC); the structural guard HIGH / OBSERVED (the `bne` edge + the `0x5040` log); the
exact Q7 poll site in this image is FLIX-desynced — MED.]**

---

## 6. Deliverable E1 — how the `0xBF` SB2SB collective rides this path

SB2SB ("State-Buffer to State-Buffer") is the on-engine collective opcode **`0xBF`**
(`NEURON_ISA_TPB_OPCODE_SB2SB_COLLECTIVE`, decoded against the 64-B `S3D3_COLLECTIVE_STRUCT`:
one 3-D SRC tensor, one 3-D DST tensor). It is hosted by **two** engines:

- the **SEQ** (`NX_POOL`) control/sequencer: `0xBF` → handler `@IRAM 0xD1E4`
  (`entry a1,32`, then `const16 a10,8`/`const16 a10,0x2d50` → LOG `"S: SB2SB_Collective"` @VA
  `0x82d50`, bytes `36 41 00 … a4 08 00 a4 50 2d` OBSERVED); it reads the 64-B word
  and **kicks** the Q7 iDMA path;
- the **POOL/Q7** data-plane (`remote_copy.cpp`): `decode_extended_inst_sb2sb` performs the
  actual cross-die SBUF→SBUF move.

The SB2SB driver (Q7 fn `@IRAM 0x3300`) lowers each leg to the two transport primitives:

```
0x3742  call8 0x161f4   ; rdma_desc_gen (op 8) — build ring + LOCAL/REMOTE sema BDs + fold dst
then    rdma_desc_start (op 9)            ; drain + PRID role-split + doorbell (§4)
```

i.e. **one SB2SB leg = one `rdma_desc_gen` + one `rdma_desc_start`**, each potentially fanning
out over up-to-16 DMA engines via `dma_engine_mask`. The full device leg is:
**PRE-SYNC → `program_window` (§2.5) → `rdma_desc_gen` → `rdma_desc_start` → two-semaphore
completion.** **[HIGH / OBSERVED — the `0x3742 → 0x161f4` call edge (bytes `25 ab 12` decoded
to `call8 0x161f4`) + the SEQ handler `@0xD1E4`; see the
[SB2SB kernel page](../firmware/kernels/sb2sb-remote-copy.md) for the full byte-exact decode of
the `0xBF` decode/validator/pre-sync.]**

---

## 7. Deliverable E2 — orchestration: host topology, NCFW steps, per-generation split

### 7.1 The full lane

```
compiler pseudo-op (TRIGGER_COLLECTIVE 0xC8 / ALL_REDUCE / SENDRECV / …)
  → host NRT lowering (TOP_SP / SPAD cc_op / doorbells)
  → TOP_SP on-device sequencer (the spad program)
  → NCFW management firmware (per-TOP_SP ctx; 4-bit algo nibble → ring / mesh / hierarchical)
  → the SB2SB/RDMA data plane (§6: program_window → gen(op8) → start(op9))
  → EVT_SEM barrier/semaphore completion (§5).
```

The HOST library (neuronx-collectives `libnccom`) builds the topology graph and decides WHICH
peers are reached over WHICH edge; the NCFW firmware sequences the STEPS within a phase; the Q7
`gen`/`start` primitives execute each step's byte movement. **[CARRIED — the collective
lane synthesis; the device legs §2–§6 are this page's OBSERVED contribution.]**

### 7.2 The host topology graph (`topo_neuron_*.cc`)

`libnccom` models the system as a graph of `NeuronNode` (a NeuronCore) connected by `NeuronEdge`,
each carrying an `EdgeLocality`. The cross-die edge type is `EdgeLocality::EdgeRemoteMLA` —
`NeuronEdge::isIntraMLA()` returns FALSE for it. An INTRA-MLA edge is on-package; an
`EdgeRemoteMLA` edge is the cross-die / cross-package **C2C** link — the host-side name for the
§3 `io_d2d` link. Per-MLA C2C port count: `caymanGetC2CPortsPerMla()` / `marianaGetC2CPortsPerMla()`
both return **4** — matching the 4 `snps_ctrl` D2D controllers in the Cayman CSR view (§3.3). The
d2d peer rule: `sengine_get_d2d_peer_nums` computes `peer = local_seng ^ 2` (toggle bit 1 of the
engine index — the host-graph analogue of the firmware's PRID-parity role split §4.2 and the
DIE-bit fold §2.3); each rank's `routing_id` is the value the host passes down that
`rdma_desc_gen` folds into the SoC high bits at off 20.

**[CARRIED — `EdgeRemoteMLA` / `isIntraMLA` / `C2CPortsPerMla=4` / `seng^2` from the
`topo_neuron_*.cc` decompile (libnccom is not in this gpsimd extraction); the **4-port ↔
4-controller** correspondence is this page's OBSERVED cross-check against the Cayman CSR view.]**

### 7.3 The per-generation orchestration split

| arch_id / gen | codename | cross-die RDMA orchestration |
|---|---|---|
| `0x05` = v2 | **SUNDA** | ships the cross-die RDMA `gen`/`start` legs + sendrecv, but **NOT** the on-engine `0xBF` SB2SB collective — SUNDA's `Q7_POOL_DEBUG_DRAM` has **0** hits for `rdma_desc_gen`/`SB2SB_Collective`/`remote_copy.cpp` (carved + counted). → cross-die RDMA via raw `gen`/`start` + sendrecv, no on-engine collapse |
| `0x0c` = v3 | **CAYMAN** | the full reference — pseudo-ops + `0xBF` SB2SB + the NCFW dispatch. `0xBF` rides `gen`/`start`; the NCFW ring/mesh/hier firmware drives per-step peer selection + the counted barrier (§5.2) |
| `0x14` = v4 | **MARIANA** ≡ `0x1c` = v4+ **MARIANA_PLUS** at the collective level — the SB2SB handler set is byte-for-name identical to CAYMAN; M+ adds a credit-gated fast-path (firmware, not ISA) |
| coretype 37 = v5 | **MAVERICK** | `0xBF` + `gen`/`start` UNCHANGED (ISA enum identical), **transport re-IP'd** (below) |

**[HIGH / OBSERVED — the SUNDA absence (0/0/0 string hits, archive carve) + the
CAYMAN reference decode; the arch_id ↔ codename binding CARRIED.]**

> **WALL — Maverick (v5) interiors are INFERRED.** Maverick's `0xBF` SB2SB op + the
> `EXTENDED_RDMA_DESC_GEN=8`/`…START=9` enum are read from its arch-isa headers (the *contract*
> is OBSERVED — the structs compile-verify `sizeof==64` identically), but its Q7 POOL firmware
> is **not in the shipped runtime archive** (no `MAVERICK_*_Q7_POOL` member, confirmed this
> session). The kernel *interior* (entry addresses, TX/RX block bytes, doorbell literals) is
> INFERRED to match the CAYMAN family by header contract.

What **is** OBSERVED for Maverick is the **transport re-IP**, byte-grounded in the Maverick
`al_address_map_db.pkl` (323 198 address-map entries) + the `amzn_ucie_*` arch-headers:

- The Cayman DWC-PCIe-over-Marvell-XSR-SerDes `io_d2d` (snps_ctrl / mrvl_xsr) → a **native UCIe**
  chiplet interconnect. The pkl enumerates **18** east-west + **8** north-south UCIe instances
  (`AMZN_UCIE_{A,S}_{EW,NS}_n` and `FIS_UCIE_*`), each carrying a `D2D_TL_WRAPPER` (transaction
  layer) + `D2D_LL_PHY_WRAPPER` (link-layer + PHY) — sample LL_PHY base `0xc00cca0000`, size
  `0x40000`. The pkl has **zero** `snps`/`mrvl`/`xsr` entries (vs Cayman's DWC-PCIe). The
  arch-headers confirm: Cayman/Mariana have **0** `ucie` files; Maverick has the
  `amzn_ucie_*/d2d_ll_phy_wrapper.h` tree.
- The **same §2 fold + §4 handshake + §5 two-semaphore protocol** ride the UCIe link instead of
  the PCIe-derived one.

**[the Maverick UCIe re-IP HIGH / OBSERVED at IP level (the `al_address_map_db.pkl` UCIe-instance
enumeration + the `amzn_ucie_*` arch-headers, both read; zero snps/mrvl in the pkl);
the **PHY / process node** is INFERRED (the customop artifacts name the IP `ucie` but do not name
the SerDes PHY); the same-protocol-over-UCIe claim is INFERRED from the unchanged ISA contract.]**

> **NOTE — capability tier.** `SUNDA ⊂ {CAYMAN ≡ MARIANA ≡ MARIANA_PLUS} ⊂ MAVERICK`. The
> collective-OP set is FLAT from CAYMAN through M+; the only post-CAYMAN collective deltas are
> Maverick's sync re-model + the D2D transport change. The `0xBF` on-engine SB2SB exists
> **CAYMAN+** (not only Maverick), riding the SAME Q7 `gen`/`start` primitives on every gen that
> has it; the NCFW ring/mesh firmware DRIVES the per-step orchestration on CAYMAN/MARIANA; SUNDA
> lacks `0xBF` and uses the raw RDMA/sendrecv legs.

---

## 8. Worked example — a cross-die SBUF→SBUF leg (one ring step)

Setup (illustrative values; the **formula + field placements are HIGH/OBSERVED**, the numeric
values are illustrative):

- Topology: a 2-die Cayman package; this core is `engine_seng = 5` on die 0 (`DIE=0`); the ring
  `next_neigh` is the paired core on die 1.
- Host: `sengine_get_d2d_peer_nums` → `peer = 5 ^ 2 = 7`; the host routing-id map sets the
  neighbor-route bits for the same-package other-die hop. The edge is `EdgeRemoteMLA`
  (`isIntraMLA() == false`) → a C2C / `io_d2d` edge (one of `caymanGetC2CPortsPerMla()=4`).
- The leg moves `free_dim_bytes = 0x4000` (16 KiB) per partition × 128 partitions from local src
  SBUF offset `0x0010_0000` to the peer's dst SBUF offset `0x0010_0000`, over
  `dma_engine_mask = 0x000F` (4 DMA engines).

**Step 1 — `rdma_desc_gen` (op 8) on both cores** (issued early, off critical path):

```c
// 64-B word: {local_sem=12, remote_sem=20, remote_core_id=7, remote_routing_id=0x12,
//             dma_engine_mask=0x000F, src_addr=0x100000, dst_addr=0x100000, free_dim_bytes=0x4000}
// die-decode (§2.4): is_die_0(peer)=0, engine_idx=7 -> cross-die -> NEIGHBOR fold mode.

uint64_t dst_soc = 0;
dst_soc = CAYMAN_ADDR_DECODE_SET_LOCAL(dst_soc, 0x100000);   // 0x00000000_00100000
dst_soc = CAYMAN_ADDR_DECODE_SET_DIE(dst_soc, 1);            // bit47 -> 0x00008000_00100000
dst_soc = CAYMAN_ADDR_DECODE_SET_EXIT_DIE(dst_soc, 1);       // bit51 -> 0x00088000_00100000
dst_soc = CAYMAN_ADDR_DECODE_SET_NEIGHBOR_ROUTE(dst_soc, 1); // bit52 -> 0x00188000_00100000
dst_soc = CAYMAN_ADDR_DECODE_SET_ID_VALID(dst_soc, 1);       // bit54 -> 0x00588000_00100000
// -> dst_soc = 0x58800000100000  (bits {20, 47, 51, 52, 54} set)
```

> **CORRECTION — the routed dst_soc is `0x58800000100000`, not `0x588000100000`.** The
> backing prose for this worked example transcribed the result with two hex zeros dropped
> (`0x588000100000` sets bits `{20, 39, 43, 44, 46}` — **wrong**). The macro-exact SET-chain
> above (verified bit-by-bit against the shipped `cayman_addr_decode_neighbor.h`
> macros) yields **`0x58800000100000`**, setting exactly `LOCAL[20]` + `DIE[47]` +
> `EXIT_DIE[51]` + `NEIGHBOR_ROUTE[52]` + `ID_VALID[54]`. The firmware's
> `after remote_routing_id=$18, dst_addr=0x58800000100000` log (DRAM `0x497b`) prints this
> corrected result. **[HIGH / OBSERVED — recomputed from the macro definitions; the dropped
> zeros are an arithmetic slip in the source prose, not a firmware fact.]**
>
> (A cross-PACKAGE peer would instead use `SET_CAYMAN_ID(dst_soc, 0x12)` `[53:48]` +
> `SET_CAYMAN_ID_VALID(dst_soc, 1)` `[54]` → the high nibble routes by chip id, giving
> `0x40128000_00100000`.)

```c
// program_window (§2.5): map a Q7-local xt_addr -> dst_soc with u/l masks; fill xt_addrs[16].
// Build, per engine, a CME-COPY BD ring copying [0x100000, 0x104000) across 128 partitions
// (buf_ptr = src SBUF SoC on the M2S/read BD, = dst_soc on the S2M/write BD), plus the
// LOCAL sema BD (EVT_SEM.inc local_sem=12 on THIS core) and the REMOTE sema BD
// (EVT_SEM.inc remote_sem=20 on the PEER, routed by routing_id=0x12 over io_d2d).
```

**Step 2 — `rdma_desc_start` (op 9) on both cores, role-split by PRID parity (§4.2):**

- **RX** (the die-1 sink) FIRST writes `S2M_Q.RDRTP_inc <- num_descriptors` (publish empty rx
  buffers), then `right_push`-signals the sender that the ring is armed.
- **TX** (the die-0 source) `left_pop`-WAITS that signal, then writes
  `M2S_Q.TDRTP_inc <- num_descriptors` — launching the CME COPY. The bytes traverse the
  iATU-OUTBOUND-mapped `io_d2d` link (DWC-PCIe over Marvell XSR) to the peer's SBUF aperture.

**Step 3 — completion (§5):**

- **LOCAL**: all 4 engines bump `EVT_SEM.inc(local_sem=12)` at `+0x1800[12*4]` on die 0 → die-0
  source buffer released.
- **REMOTE**: when all 128 × 16 KiB land on die 1, the routed REMOTE sema BD does
  `EVT_SEM.inc(remote_sem=20)` at the peer's `+0x1800[20*4]` → die-1 dst buffer ready to read.
- The next ring step's `rdma_desc_start` on die 1 does `add_semaphore_wait_ge_and_dec`
  (`remote_sem ≥ target`) before issuing — chaining the legs (NCFW counted barrier).

**[INFERRED-HIGH — the §2.3 SET-chain arithmetic + the §4/§5 sequence are HIGH/OBSERVED; the
specific numeric values (cores, routing_id, sizes) are illustrative; the corrected `dst_soc`
is bit-verified above.]**

---

## 9. Reconciliation table (this page ↔ sources)

| fact | source | status |
|---|---|---|
| `rdma_desc_gen @0x161f4` / `start @0x1723c` (entry bytes `36 81 4a` / `36 01 02`) | `q7_iram.bin` | CONFIRMED byte-exact |
| role parity `extui`/`bne`/`beqz.n` + both `39 04` stores | native `ncore2gp` objdump | CONFIRMED byte-exact |
| `call8 0x161f4` @`0x3742` (bytes `25 ab 12`) | decoded | CONFIRMED (imm18→0x161f4) |
| TX="M2S"/RX="S2M" DRAM tokens @`0x487f`/`0x4d8c` | `q7_dram.bin` `xxd` | CONFIRMED byte-exact |
| gen logs routing_id / local+remote sema BDs @`0x497b`/`0x4bd6`/`0x4c2a` | `q7_dram.bin` | CONFIRMED byte-exact |
| `is_die_0`/`engine_idx` die-decode str @`0x0f98` | `q7_dram.bin` | CONFIRMED |
| `program_window`/`update_window` strs @`0x0ffd`/`0x1856` | `q7_dram.bin` | CONFIRMED + soc_addr/mask |
| SoC local `{DIE[47],CAYMAN_ID[53:48],_VALID[54]}` | `cayman_addr_decode.h` macros | CONFIRMED (macro-exact) |
| neighbor `{EXIT_SENG[50],EXIT_DIE[51],NEIGHBOR_ROUTE[52],PEB[53],ID_VALID[54]}` | `cayman_addr_decode_neighbor.h` macros | CONFIRMED (macro-exact) |
| `io_d2d` = DWC-PCIe (`snps_ctrl`) + Marvell XSR + iATU OUTBOUND | `csrs/d2d/*.json` | CONFIRMED (PCIe caps + iATU regs) |
| d2d INTC carries 0 data-plane triggers (216 total) | `intc/d2d_triggers.yaml` | CONFIRMED |
| `TDRTP_inc`/`RDRTP_inc` `+0x038` doorbells, `ArraySize 16` | CSR JSON (gen/start page §4.7) | CONFIRMED |
| `dma_apb_bcast{m2s_tail_ptr,s2m_tail_ptr,mask}` | NCFW ring-channel config | CARRIED |
| EVT_SEM inc window `+0x1800` / 256 sema | `csrs/tpb/tpb_events_semaphores_axi.json` | CONFIRMED (offset in JSON + fw) |
| `0xBF` SB2SB → `call8 0x161f4` ; SEQ handler @`0xD1E4` | `nx_iram.bin`/`q7_iram.bin` | CONFIRMED byte-exact |
| `EdgeRemoteMLA` / `C2CPortsPerMla=4` / d2d peer = `seng^2` | `topo_neuron_*.cc` (libnccom) | CARRIED |
| SUNDA lacks `0xBF` (0/0/0 string hits) | archive carve | CONFIRMED |
| Maverick UCIe re-IP (`amzn_ucie_*`, `D2D_TL/LL_PHY_WRAPPER`, 0 snps/mrvl) | `al_address_map_db.pkl` + arch-headers | CONFIRMED (IP level); PHY INFERRED |
| worked `dst_soc = 0x58800000100000` | recomputed from macros | **CORRECTED** (source prose dropped 2 hex zeros) |

---

## 10. Confidence / gaps

**HIGH / OBSERVED** — the SoC field codec (both decoders, macro-exact); the `gen`/`start` entries
+ role-parity split + both `s32i.n a3,a4,0` tail-inc stores + the `[TX]`/`[RX]` logs + the DRAM
`TX`/`RX`/`M2S`/`S2M` tokens (native objdump); the gen local/remote sema-descriptor
logs (routing_id on the remote one); the die-decode + program_window logs; the `io_d2d` IP makeup
(DWC-PCIe + Marvell XSR + MPCS/ULFEC, iATU OUTBOUND, 216-trigger INTC carrying zero data-plane
triggers); the EVT_SEM `+0x1800` 256-sema INC window; the `0xBF` SB2SB → `gen`/`start`
decomposition + the SEQ handler `@0xD1E4`; the SUNDA absence proof; the Maverick UCIe-instance
enumeration from the pkl.

**MED** — the exact branch (chip-id vs neighbor fold) per peer (host-decided); the `left_pop`=wait
/ `right_push`=signal direction (log wording + ring semantics); the absolute `a4` doorbell value
(negative PC-relative literal — bound by NCFW `dma_apb_bcast`); the device-side `is_die_0`/
`engine_idx` bit-extract body + the per-engine sema-BD push sites (FLIX-desynced); the iATU-region
↔ neighbor-decoder binding; the EVT_SEM per-sema target address of the remote BD; the 4-controller
vs 8-subsystem reconciliation.

**LOW / CARRIED** — the absolute remapper / EVT_SEM peer-aperture register offsets (negative
literals, not in this image); the host topology facts (`EdgeRemoteMLA`/`C2C`/`seng^2`, CARRIED —
libnccom not in this extraction); the §8 worked-example numeric values (illustrative; the
SET-chain arithmetic + protocol sequence are HIGH).

**WALL** — every Maverick (v5) *interior* claim is INFERRED (no shipped Q7 firmware); the v5 D2D
**PHY** is not named in the customop artifacts and is INFERRED at IP level from the `amzn_ucie_*`
block names; the same-protocol-over-UCIe claim is INFERRED from the unchanged ISA contract.

---

## Cross-references

- [SB2SB Remote-Copy Collective](../firmware/kernels/sb2sb-remote-copy.md) — the `0xBF` collective
  (the SEQ `0xBF` + Q7 `remote_copy` path) that lowers to `gen`/`start`; the call edge
  `0x3742 → 0x161f4` is byte-confirmed there.
- [RDMA Descriptor Gen/Start](../firmware/kernels/rdma-desc-gen-start.md) — the byte-exact
  `rdma_desc_gen`(op8) / `rdma_desc_start`(op9) decode this page routes.
- [CCE (Compute-DMA) In-Transfer Compute](cce-in-transfer.md) — the sibling in-transfer-compute
  DMA path.
- [S3D3 Collective (SB2SB, 0xBF)](../collectives/ops/s3d3-collective.md) — the host-side
  collective-op view.
- [The Unified Collective-Communication Architecture](../collectives/ops/architecture-synthesis.md)
  — the algorithm-selection / per-step orchestration synthesis.
- [NCFW DMA Reprogram + APB Broadcast](../collectives/ncfw/dma-reprogram-apb-bcast.md) — supplies
  the `dma_apb_bcast{m2s_tail_ptr,s2m_tail_ptr,mask}` doorbell SoC addrs + per-step peers.
- [CSR — HBM / D2D / PCIe Blocks](../control/csr/hbm-d2d-pcie-blocks.md) — the register-level
  `snps_ctrl` / `d2d_ctrl_axi_cfg` / `mrvl_xsr_phy` decode + the d2d triggers.
- [pkl PCIe / D2D / Fabric Subtree](../control/address/pkl-pcie-d2d-fabric.md) — the
  `al_address_map_db.pkl` UCIe/D2D address-map subtree.
- [Confidence & Walls Model](../reference/confidence-model.md) — the tag system used throughout.
