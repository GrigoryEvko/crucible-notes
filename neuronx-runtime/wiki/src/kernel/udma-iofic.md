# UDMA IOFIC Interrupt Controller

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0** (`usr/src/aws-neuronx-2.27.4.0/`). The two owned files are `udma/udma_iofic.c` (412 lines) and the IOFIC half of `udma/udma_regs.h` (the `iofic_grp_ctrl` / `union iofic_regs` / `udma_iofic_regs` block and the gen-space register map, 668 lines total), both read verbatim; the `struct udma` fields the policy layer dereferences (`gen_int_regs`, `rev_id`) are cited at `udma/udma.h` and owned by the [UDMA core](udma-main.md). Every offset below is a struct field or a macro, not a recovered binary offset; the driver ships as a stripped `.ko` but the GPL C is authoritative. Other driver versions renumber lines.*
> *Evidence grade: **Confirmed (source-anchored)** — full C source, no decompilation. This page owns the **authoritative kernel register layout** of the IOFIC; the userspace `libnrt.so` view (the `al_iofic_*` byte-anchored disassembly) is owned by [dma/iofic](../dma/iofic.md) and cross-linked, not re-derived. The two agree on the group/level/offset model. · Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

The UDMA **IOFIC** — I/O Fabric Interrupt Controller — is the AnnapurnaLabs/Alpine micro-DMA interrupt aggregator, and `udma_iofic.c` is the kernel half of it: the layer that programs the controller during engine bring-up so the silicon **aborts a faulting DMA** and summarizes one MSI-X for an error handler outside this cell. A reader who knows any "interrupt controller with a per-source cause/mask/unmask register file" — a GIC distributor, an APIC, an MSI-X capability — already owns the frame. The decisive structural fact is that the IOFIC is **two controllers stacked**: a **primary** ("main") level with four group summaries A–D, where group D is the funnel that summarizes the **secondary** level, and a **secondary** level where every individual UDMA error/abort cause physically lives (group A = M2S/Tx errors, group B = S2M/Rx errors, group C = shared M2S+S2M AXI-fifo parity). Unmasking a secondary cause is invisible at the top until primary group D is also configured and unmasked — the funnel must be lifted.

The defining fact about the kernel IOFIC, stated by what the driver *does not* do, is that the entire `udma_iofic.c` surface is wired for the **error / abort** path only. There is no ISR, no `int_cause_grp` read, no completion-IRQ servicing anywhere in the cell. Completion on the Neuron M2M path is observed by **polling a host-memory marker** (`cdesc_base == NULL` on every M2M queue, owned by [udma-m2m](udma-m2m.md) and [dma-rings](dma-rings.md)), not by a completion queue and not by an interrupt. The IOFIC is configured exactly once per engine — in `udma_init` → `udma_iofic_m2s_error_ints_unmask` / `_s2m_error_ints_unmask` (`udma_main.c:350–351`, the [UDMA core](udma-main.md) boundary) — for posedge-latched, MSI-X-coalesced delivery of parity faults, AXI timeouts, and ring-id mismatches, and is otherwise off the data path. The `INT_CONTROL_GRP_MASK_MSI_X` programming means a fired error *summary* collapses to a single MSI-X; the busy copy path never blocks on it.

This page documents four artifacts a reimplementer must reproduce: (1) the **per-group register window** — a `0x40`-byte `iofic_grp_ctrl` control block indexed `ctrl[group]`, with cause at `+0x00`, mask at `+0x10`, mask-clear at `+0x18`, control at `+0x28`, abort-mask at `+0x30`; (2) the **two-level container geometry** — primary level `union iofic_regs main_iofic` at offset `0x0` (size `0x400`, 16 group slots), a `0x1c00` gap, then `secondary_iofic_ctrl[]` at `0x2000`; (3) the **rev-gated group-validity model** — primary groups `0..3` always, secondary groups `0..1` below rev 4 and `0..2` (adding group C) at rev ≥ 4, the only silicon shipped; and (4) the **error/abort cause catalog** — the per-group cause bitmasks (`UDMA_IOFIC_2ND_GROUP_A_ERROR_INTS` and its B/C peers) and the `config → abort_mask_clear → unmask → lift-D` policy that arms them. The bit *meanings* are the authoritative kernel catalog this page owns; the userspace [dma/iofic](../dma/iofic.md) page treats them as opaque.

For reimplementation, the contract is:

- **The group window** — each IOFIC group is a `0x40`-byte `iofic_grp_ctrl` block reached as `((union iofic_regs *)base)->ctrl[group]`; the four fields the driver touches are `int_control_grp@+0x28` (config), `int_mask_clear_grp@+0x18` (unmask), `int_abort_msk_grp@+0x30` (abort-arm), and the cause/status/mask fields it declares but never reads on this path.
- **The write conventions** — `iofic_unmask` writes **`~mask`** to `int_mask_clear_grp` (set bit in `mask` ⇒ unmasked); `iofic_abort_mask_clear` writes **`~mask`** to `int_abort_msk_grp` (set bit in `mask` ⇒ engine aborts on that cause). A reimplementer who writes `mask` (not `~mask`) inverts the controller.
- **The two-level model** — primary `{A=0, B=1, C=2, D=3}` with D the secondary funnel (`INT_GROUP_D_M2S`=bit8, `INT_GROUP_D_S2M`=bit9, `INT_UDMA_V4_GROUP_D_2ND_IOFIC_GROUP_C`=bit10); secondary `{A=0 (M2S err), B=1 (S2M err), C=2 (shared AXI parity)}`; group C exists only on rev ≥ 4.
- **The error-only policy** — `iofic_config(grp, POSEDGE|MASK_MSI_X)` → `iofic_abort_mask_clear(grp, bits)` → `iofic_unmask(grp, bits)` per secondary group, then `iofic_unmask(PRIMARY, D, funnel_bits)`. Completion is **not** in this path.

| | |
|---|---|
| **Source** | `udma/udma_iofic.c` (412 lines), IOFIC block of `udma/udma_regs.h` (668 lines), aws-neuronx-dkms 2.27.4.0, GPL-2.0 |
| **Levels** | two — **primary** (groups A–D, D = secondary funnel) and **secondary** (A/B/C = M2S err / S2M err / shared AXI parity) |
| **Group window** | `struct iofic_grp_ctrl`, `0x40` bytes, `ctrl[group]` stride (`udma_regs.h:492–517`) |
| **Field offsets** | `int_cause_grp +0x00` · `int_mask_grp +0x10` · `int_mask_clear_grp +0x18` · `int_control_grp +0x28` · `int_abort_msk_grp +0x30` |
| **Container** | `main_iofic @0x0` (size `0x400`) · `0x1c00` gap · `secondary_iofic_ctrl[] @0x2000` (`udma_regs.h:524–528`) |
| **Control flag** | `INT_CONTROL_GRP_SET_ON_POSEDGE`(bit3, `:232`) `\|` `INT_CONTROL_GRP_MASK_MSI_X`(bit5, `:237`) — group summary → one MSI-X |
| **Unmask convention** | `iofic_unmask` writes **`~mask`** to `int_mask_clear_grp@+0x18` (`:259–263`) |
| **Abort convention** | `iofic_abort_mask_clear` writes **`~mask`** to `int_abort_msk_grp@+0x30` (`:273–277`) ⇒ engine aborts on those causes |
| **Rev gate** | secondary group count = 2 if `rev_id < UDMA_REV_ID_4`, else 3 (`:312–315`); `UDMA_REV_ID_4 = 4` (`udma.h:30`) |
| **Public entry points** | `udma_iofic_m2s_error_ints_unmask` (`:345`) · `_s2m_error_ints_unmask` (`:375`) · `_error_ints_unmask_one` (`:406`) |
| **Completion role** | **none** — completion is the host-memory marker poll ([udma-m2m](udma-m2m.md), [dma-rings](dma-rings.md)); no ISR / cause read in this cell |
| **Up boundary** | engine bring-up caller `udma_init` (`udma_main.c:350–351`) → [udma-main](udma-main.md) |
| **Side boundary** | the 6 gen-space AXI sub-IOFICs + ring-id-error mask → [udma-m2m](udma-m2m.md) |
| **Down boundary** | userspace `al_iofic_*` byte-anchored view → [dma/iofic](../dma/iofic.md) (do **not** re-derive) |

---

## 1. The Two-Level Topology

### Purpose

Before any cause table makes sense, the funnel must be clear. The IOFIC is two controllers: a primary level whose four group summaries A–D are what an MSI-X actually carries, and a secondary level that holds the individual error causes. Primary group D is the only path by which a secondary cause reaches the top — the secondary controller does not raise an MSI-X of its own; it raises a *summary bit* into primary D, which then summarizes into the one MSI-X. A reimplementer who arms a secondary cause and stops has armed nothing visible: the funnel discipline (`unmask secondary cause`, then `unmask primary D's summary bit`) is mandatory and is the shape of every entry point in §3.

### The funnel

```text
                         ┌──────────────────────────────────────────────┐
   one MSI-X  ◄──────────┤  PRIMARY IOFIC   main_iofic @ gen+0x0  (0x400)│
                         │     group A (0)  = "summary"                  │  ← never unmasked
                         │     group B (1)  = RX completion              │  ← never unmasked
                         │     group C (2)  = TX completion              │  ← never unmasked
                         │     group D (3)  = MISC / FUNNEL              │
                         └───────────────────┬──────────────────────────┘
                                             │  D's summary bits:
                                             │    bit8  INT_GROUP_D_M2S        (sec grp A)
                                             │    bit9  INT_GROUP_D_S2M        (sec grp B)
                                             │    bit10 ..._2ND_IOFIC_GROUP_C  (sec grp C)
                         ┌───────────────────┴──────────────────────────┐
                         │  SECONDARY IOFIC   secondary_iofic_ctrl @ gen+0x2000
                         │     group A (0) = M2S (Tx) error causes  ─┐    │
                         │     group B (1) = S2M (Rx) error causes  ─┤ where every parity /
                         │     group C (2) = shared AXI-fifo parity ─┘ AXI-timeout / ring-id
                         │                   (rev >= 4 only)           cause physically lives
                         └──────────────────────────────────────────────┘
```

The primary group A/B/C labels ("summary", "RX completion", "TX completion") come from the file-header comment (`udma_iofic.c:14–20`) and are *(LOW confidence)* — the driver never programs primary A/B/C, so their HW semantics are unverified against behavior. What *is* certain is that they are never unmasked on the Neuron path (§4): only group D is, and only its three secondary-funnel bits. The completion groups B and C of the *primary* level are exactly the groups a completion-IRQ-driven design would use — and the driver leaves them masked, which is the structural fingerprint of the polled-completion model (§4).

> **NOTE —** "MISC / FUNNEL" for primary group D is the role the driver actually exercises, not the header label. The three bits the driver sets into D's mask are `INT_GROUP_D_M2S` (bit 8 = secondary group A summary, `:23`), `INT_GROUP_D_S2M` (bit 9 = secondary group B summary, `:25`), and `INT_UDMA_V4_GROUP_D_2ND_IOFIC_GROUP_C` (bit 10 = secondary group C summary, `:28`). The header comment at `:27` states bits 8–11 of group D connect to the secondary level's groups A–D; the driver uses only bits 8–10 because the secondary controller has at most three groups (A/B/C) on this silicon.

---

## 2. The Register Block Layout

### Purpose

Every IOFIC operation is an access to one *group's* `0x40`-byte control block, reached by casting the resolved level base to `union iofic_regs *` and indexing `ctrl[group]` (`iofic_config`/`iofic_unmask`/`iofic_abort_mask_clear` all do exactly this, `:249`/`:261`/`:275`). Pinning the `iofic_grp_ctrl` field offsets and the two-level container geometry is the whole of the layout a reimplementer needs; the surrounding M2S/S2M/gen register files are the [UDMA core](udma-main.md)'s map.

### The per-group control block — `struct iofic_grp_ctrl`

`struct iofic_grp_ctrl` (`udma_regs.h:492–517`) is one IOFIC group's register window: 13 named/reserved `u32` words plus a 3-word reserved tail, exactly `0x40` (64) bytes, so `ctrl[group]` lands group N at `N * 0x40`.

| Field | Offset | Reg role | Driver use | Confidence |
|---|---|---|---|---|
| `int_cause_grp` | `+0x00` | Interrupt Cause (RW1C; the ISR read/ack target) | declared, **never read** in this cell | HIGH |
| `int_cause_set_grp` | `+0x08` | Cause Set — write 1 ⇒ set a cause bit (SW-generated IRQ) | unused | HIGH |
| `int_mask_grp` | `+0x10` | Interrupt Mask (1 = masked) | RMW-OR by the ring-id-error mask path ([udma-m2m](udma-m2m.md)) | HIGH |
| `int_mask_clear_grp` | `+0x18` | Mask **Clear** — write `~mask` ⇒ unmask the set bits | `iofic_unmask` target (`:262`) | HIGH |
| `int_status_grp` | `+0x20` | Interrupt Status | declared, never read in this cell | HIGH |
| `int_control_grp` | `+0x28` | Interrupt Control (posedge / MSI-X mode) | `iofic_config` target (`:250`) | HIGH |
| `int_abort_msk_grp` | `+0x30` | **Abort** Mask — write `~mask` ⇒ engine aborts on those causes | `iofic_abort_mask_clear` target (`:276`) | HIGH |
| `reserved7[3]` | `+0x34` | 12-byte tail | — | HIGH |

> **CORRECTION (K-IOFIC-1) — the `+0x30` comment mislabels the abort-mask register.** The source comment above `int_abort_msk_grp` reads `/* [0x30] Interrupt Mask Register */` (`udma_regs.h:514`), identical to the `int_mask_grp@+0x10` comment. This is a copy-paste error in the GPL header, not two mask registers. The field is the **abort** mask: its *name* is `int_abort_msk_grp`, and its sole writer is `iofic_abort_mask_clear` (`udma_iofic.c:273–277`), whose own doc comment states *"This will result in UDMA aborting on the specific interrupts"* (`:266–267`). A reimplementer who trusts the `+0x30` field comment will conflate the delivery mask (`+0x10`) with the abort mask (`+0x30`) — they are distinct registers with distinct effects (suppress-delivery vs. abort-the-engine). Confidence the field is the abort mask: **HIGH** (field name + usage); confidence the comment is wrong: **HIGH**.

### The two-level container — `union iofic_regs` and `struct udma_iofic_regs`

One IOFIC *level* is `union iofic_regs` (`udma_regs.h:519–522`): a `ctrl[16]` array of `iofic_grp_ctrl` (16 × `0x40` = `0x400`) overlaid with a `reserved0[0x400>>2]` word array that pins the union size to exactly `0x400`. The two-level container `struct udma_iofic_regs` (`udma_regs.h:524–528`) then stacks them:

```text
struct udma_iofic_regs   (lives at gen+0x0 inside udma_gen_regs_v4; gen @ unit+0x38000)
  +0x0000  union iofic_regs main_iofic            PRIMARY level   (size 0x400, ctrl[16])
  +0x0400  u32 reserved0[0x1c00 >> 2]             0x1c00-byte gap
  +0x2000  struct iofic_grp_ctrl secondary_iofic_ctrl[2]   SECONDARY level   (0x40 each)
           └── 0x400 + 0x1c00 = 0x2000  (secondary base, VERIFIED arithmetic)
```

`udma_iofic_reg_base_get_adv` (`:288–296`) resolves a level to its base: primary returns `&int_regs->main_iofic`, secondary returns `&int_regs->secondary_iofic_ctrl` (`:294`/`:296`). Both are then cast to `union iofic_regs *` inside the three poke helpers and indexed `ctrl[group]` — so secondary group N also computes `secondary_base + N * 0x40`, even though the secondary base is declared as a bare `iofic_grp_ctrl[2]` rather than a `union iofic_regs`.

> **GOTCHA — the secondary array under-declares group C.** `secondary_iofic_ctrl` is declared `[2]` (`udma_regs.h:528`) — slots for groups A and B only. But the rev-4 path drives secondary group **C** (index 2): `udma_iofic_m2s_error_ints_unmask` calls `iofic_abort_mask_clear(secondary, INT_GROUP_C, ...)` (`:362`) and `udma_iofic_unmask_adv(secondary, INT_GROUP_C, ...)` (`:369`), with `INT_GROUP_C = 2` (`:224`). Because every helper reinterprets the base as `union iofic_regs *` and indexes `ctrl[2]`, the access lands at `secondary_base + 0x80` — the memory immediately *after* `secondary_iofic_ctrl[1]`. The C struct under-declares the array; the hardware lays out ≥ 3 contiguous secondary blocks at `0x2000`. A reimplementer who sizes the secondary array to the declared `[2]` and bounds-checks against it will reject the (valid, rev-4) group-C access. Size the secondary region to hold three `iofic_grp_ctrl` blocks. The funnel is consistent: `udma_m2m_mask_ring_id_error` independently indexes the primary level by `2*sizeof(iofic_grp_ctrl)` (group C) and `3*sizeof` (group D), confirming the `0x40` stride and that `secondary[0]=M2S`, `[1]=S2M` directly ([udma-m2m](udma-m2m.md)). Confidence the code accesses `0x2000+0x80`: **HIGH**; that a physical third block exists there: **MEDIUM** (inferred from the access, not stated by the struct).

### The gen-space AXI sub-IOFICs

Beyond the two-level controller, the gen register file (`struct udma_gen_regs_v4`, `udma_regs.h:594–620`) carries six **per-direction AXI error sub-IOFICs**, each a single base word followed by `rsrvd[63]` (a `0x100` stride):

| Gen offset | Field | Direction |
|---|---|---|
| `+0x2d00` | `iofic_base_m2s_desc_rd` | M2S descriptor read |
| `+0x2e00` | `iofic_base_m2s_data_rd` | M2S data read |
| `+0x2f00` | `iofic_base_m2s_cmpl_wr` | M2S completion write |
| `+0x3000` | `iofic_base_s2m_desc_rd` | S2M descriptor read |
| `+0x3100` | `iofic_base_s2m_data_wr` | S2M data write |
| `+0x3200` | `iofic_base_s2m_cmpl_wr` | S2M completion write |

These six base words are each cast to `struct iofic_grp_ctrl *` and **blanket-unmasked** (`mask = 0xffffffff`) by `udma_iofic_error_ints_unmask_one` (§3), called 6× from `udma_m2m_set_axi_error_abort` ([udma-m2m](udma-m2m.md), `udma_m2m.c:479–484`). The per-bit cause semantics of these sub-IOFICs are not enumerated by this cell — the driver arms all 32 bits unconditionally — so their cause catalog is *(LOW confidence, not traced)*; this page owns only their offsets and the unmask call.

### Init pseudocode — the three register pokes

```c
// iofic_config — udma_iofic.c:247.  Set HOW a group's causes latch/deliver.  Still MASKED after.
function iofic_config(regs_base, group, flags):
    regs = (union iofic_regs *) regs_base               // :249  cast level base
    reg_write32(&regs->ctrl[group].int_control_grp, flags)   // :250  WHOLE int_control_grp@+0x28
    // flags = SET_ON_POSEDGE(bit3) | MASK_MSI_X(bit5) = 0x28 on every error-unmask path.

// iofic_unmask — udma_iofic.c:259.  ARM causes for delivery (atomic vs HW auto-mask).
function iofic_unmask(regs_base, group, mask):
    regs = (union iofic_regs *) regs_base
    reg_write32(&regs->ctrl[group].int_mask_clear_grp, ~mask)   // :262  ~mask: SET bit => UNMASK
    // mask-CLEAR register (+0x18), not the mask register (+0x10): write is atomic against
    // the HW auto-mask and a concurrent CPU (doc :255-256).

// iofic_abort_mask_clear — udma_iofic.c:273.  ARM engine abort on selected causes.
function iofic_abort_mask_clear(regs_base, group, mask):
    regs = (union iofic_regs *) regs_base
    reg_write32(&regs->ctrl[group].int_abort_msk_grp, ~mask)   // :276  ~mask: CLEAR abort-mask bit
    // clearing an abort-mask bit ARMS abort: the UDMA aborts the engine when that cause fires.

// reg_write32 == writel (K-REG-ACCESS boundary, neuron_reg_access.c).  No barrier here; the
// IOFIC is configured once at bring-up, off the doorbell ordering path.
```

> **GOTCHA — two registers, two inversions.** `iofic_unmask` and `iofic_abort_mask_clear` both write the **complement** of their `mask` argument (`~mask`, `:262`/`:276`), to two *different* "clear" registers (`int_mask_clear_grp@+0x18` and `int_abort_msk_grp@+0x30`). The call-site convention is uniform — *"set bit in `mask` ⇒ enable the behavior"* — but the register-level semantics are inverted. A reimplementer who writes `mask` directly to `int_mask_clear_grp` unmasks exactly the causes the caller meant to leave masked; one who writes `mask` (not `~mask`) to `int_abort_msk_grp` disarms abort for every cause *not* named. Match register to operation: configure is `int_control_grp@+0x28` (whole word); unmask is `int_mask_clear_grp@+0x18` (`~mask`); abort-arm is `int_abort_msk_grp@+0x30` (`~mask`). This is the same inversion the userspace twin carries ([dma/iofic §2](../dma/iofic.md#2-the-cause--mask--unmask-primitives)).

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `iofic_config` | `udma_iofic.c:247` | static: write `int_control_grp@+0x28 = flags` (posedge/MSI-X mode) | HIGH |
| `iofic_unmask` | `udma_iofic.c:259` | static: write `~mask` to `int_mask_clear_grp@+0x18` (arm delivery) | HIGH |
| `iofic_abort_mask_clear` | `udma_iofic.c:273` | static: write `~mask` to `int_abort_msk_grp@+0x30` (arm engine abort) | HIGH |
| `udma_iofic_reg_base_get_adv` | `udma_iofic.c:288` | static: resolve level → `&main_iofic` (primary) or `&secondary_iofic_ctrl` (secondary) | HIGH |

---

## 3. The Error / Abort Cause Model

### Purpose

The secondary level is where the catalog lives. Each secondary group is one `iofic_grp_ctrl` whose 32 cause bits name specific fault classes; the driver aggregates the bits it cares about into a per-group mask macro and arms the whole mask in one `abort_mask_clear` + one `unmask`. This section is the authoritative kernel cause catalog — the userspace [dma/iofic](../dma/iofic.md) page treats these masks as opaque policy values; here they are decoded bit-by-bit.

### Group A — M2S (Tx) error causes

`UDMA_IOFIC_2ND_GROUP_A_ERROR_INTS` (`udma_iofic.c:172–186`) is the OR of all 28 M2S error causes (`:31–86`), bits 0..27. It is the mask passed to both the group-A `abort_mask_clear` (`:359`) and the group-A `unmask` (`:366`) in the M2S path:

| Bits | Cause class |
|---|---|
| `27..24` | MSI-X response/timeout (`MSIX_RESP`, `MSIX_TO`); prefetch header/descriptor parity (`PREFETCH_HDR_PARITY`, `PREFETCH_DESC_PARITY`) |
| `23..18` | data/header/completion-coalescing/unack/ack/AXI-data parity (`DATA_PARITY` … `AXI_DATA_PARITY`) |
| `17..13` | prefetch ring-id / last / first / max-desc; packet length (`PREFETCH_RING_ID`, `PKT_LEN`, …) |
| `12..10` | prefetch AXI timeout / response / parity (`PREFETCH_AXI_TO/RESP/PARITY`) |
| `9..7` | data AXI timeout / response / parity (`DATA_AXI_TO/RESP/PARITY`) |
| `6..4` | completion AXI timeout / response / parity (`COMPL_AXI_TO/RESP`, `COMP_AXI_PARITY`) |
| `3..0` | stream timeout / response / parity; stream-completion mismatch (`STRM_TO/RESP/PARITY`, `STRM_COMPL_MISMATCH`) |

The bits fall into four families that recur across all three groups: **AXI faults** (timeout / response-error / parity, on each of the prefetch / data / completion sub-channels), **internal SRAM/FIFO parity** (header, descriptor, packet), **stream/protocol violations** (length, first/last, completion mismatch), and **MSI-X delivery faults**. A reimplementer wiring abort-on-error arms the whole `0x0FFFFFFF`-class swath; the userspace policy writes exactly that coarse mask ([dma/iofic §4](../dma/iofic.md#4-the-error-unmask-policy-composition)).

### Group B — S2M (Rx) error causes

`UDMA_IOFIC_2ND_GROUP_B_ERROR_INTS` (`:188–201`) is the base S2M mask; the rev-4 superset `UDMA_V4_IOFIC_2ND_GROUP_B_ERROR_INTS` (`:203–204`) adds bit 31 (`TRNSCTN_TBL_INFO_PARITY`, a V4-only transaction-table parity cause). The full table (`:89–142`) is the Rx mirror of group A:

| Bits | Cause class |
|---|---|
| `31` | transaction-table-info parity (`TRNSCTN_TBL_INFO_PARITY`) — **V4 only** |
| `30..24` | prefetch-desc / completion-coalescing / (pre-)unack / data / data-hdr parity; packet length |
| `23..17` | stream last/first/data/hdr (+parity); completion-unack |
| `16..13` | completion stream; completion AXI timeout / response / parity |
| `11..8` | prefetch ring-id; prefetch AXI timeout / response / parity |
| `2..0` | data AXI timeout / response / parity (`DATA_AXI_TO/RESP/PARITY`) |

> **NOTE —** the group-B table has gaps at bits 12 and 3–7 (no cause defined there). The driver's mask is the OR of the *defined* bits, so the reserved positions are never armed. A reimplementer who arms `0xFFFFFFFF` on group B (rather than the explicit OR) arms reserved bits whose abort behavior is unspecified; build the mask from the named causes, not from "all ones". The V4-only bit 31 is the one rev-keyed difference — `_s2m_error_ints_unmask` (`:381`) uses the `_V4_` superset macro.

### Group C — shared M2S + S2M AXI-fifo parity (rev ≥ 4)

Secondary group C is the shared parity group, split into two rev-4 masks so the M2S and S2M paths each arm only their own half:

| Macro | Source | Bits | Causes |
|---|---|---|---|
| `UDMA_V4_IOFIC_2ND_GROUP_C_M2S_ERROR_INTS` | `:206–211` | `0..5` | M2S data-fetch / descriptor-prefetch / completion-write AXI cmd+req parity |
| `UDMA_V4_IOFIC_2ND_GROUP_C_S2M_ERROR_INTS` | `:213–217` | `6..11` | S2M data-write / completion-write / descriptor-prefetch AXI data/cmd+req parity |

The M2S path arms bits 0–5 of group C (`:362`/`:369`), the S2M path arms bits 6–11 (`:394`/`:400`). Group C is the funnel's third bit (`INT_UDMA_V4_GROUP_D_2ND_IOFIC_GROUP_C`, bit 10), and is the reason both directions' primary-D masks carry that bit.

### The rev-gated validity model

Which secondary groups are *valid* is rev-keyed by `udma_iofic_level_and_group_valid` (`:307–322`): primary always allows `0..3`; secondary allows `0..(sec_group-1)` where `sec_group = 2` below rev 4 and `3` at rev ≥ 4. This is the gate that admits secondary group C only on rev-4 silicon.

```c
// udma_iofic_level_and_group_valid — udma_iofic.c:307.  Returns 1 valid / 0 invalid.
function udma_iofic_level_and_group_valid(udma, level, group):
    sec_group = (udma->rev_id < UDMA_REV_ID_4) ? 2 : 3       // :312-315  rev gate (UDMA_REV_ID_4 == 4)
    if level == UDMA_IOFIC_LEVEL_PRIMARY and 0 <= group < 4:        return 1   // :317  A..D always
    if level == UDMA_IOFIC_LEVEL_SECONDARY and 0 <= group < sec_group: return 1 // :318  A,B (+C at rev>=4)
    return 0                                                  // :321

// udma_iofic_unmask_adv — udma_iofic.c:334.  The VALIDATED unmask wrapper.
function udma_iofic_unmask_adv(udma, level, group, mask):
    if not udma_iofic_level_and_group_valid(udma, level, group):    // :337
        pr_err("invalid iofic level(%d) or group(%d)", level, group) // :338
        return
    iofic_unmask(udma_iofic_reg_base_get_adv(udma, level), group, mask)  // :342
```

> **GOTCHA — the abort path is not validated; only the unmask path is.** `udma_iofic_unmask_adv` (`:334`) gates every unmask through `udma_iofic_level_and_group_valid`, so an out-of-range or rev-stale group is rejected with a `pr_err` and *skipped*. But `iofic_abort_mask_clear` is called **directly** on the resolved base (`:359`/`:362`/`:391`/`:394`) with **no validity check** — there is no `_adv` wrapper on the abort path. On rev < 4, secondary group C (index 2) is invalid, yet the abort_mask_clear for group C would still write `secondary_base + 0x80`. This is harmless on the only shipped silicon (rev = 4, where group C is valid), but a reimplementer who back-ports to a rev-< 4 part must add the rev gate to the abort path themselves, or the abort-mask write lands on a non-existent group block. Confidence the unmask path validates and the abort path does not: **HIGH** (the call sites differ structurally — `udma_iofic_unmask_adv` vs. bare `iofic_abort_mask_clear`).

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `udma_iofic_level_and_group_valid` | `udma_iofic.c:307` | static: rev-gated group-validity (primary 0..3; secondary 0..1, +C at rev≥4) | HIGH |
| `udma_iofic_unmask_adv` | `udma_iofic.c:334` | static: validate (level,group) then `iofic_unmask`; `pr_err` + skip on invalid | HIGH |
| `UDMA_IOFIC_2ND_GROUP_A_ERROR_INTS` | `udma_iofic.c:172` | M2S error mask (28 causes, bits 0..27) | HIGH |
| `UDMA_V4_IOFIC_2ND_GROUP_B_ERROR_INTS` | `udma_iofic.c:203` | S2M error mask (base + V4 bit31) | HIGH |
| `UDMA_V4_IOFIC_2ND_GROUP_C_{M2S,S2M}_ERROR_INTS` | `udma_iofic.c:206`/`:213` | shared AXI-parity masks (M2S bits0..5 / S2M bits6..11) | HIGH |

---

## 4. The Error-Unmask Policy and the Polled-Completion Quirk

### Purpose

The two public error-unmask entry points compose §2's pokes into the actual bring-up sequence. Their shape is invariant: for each secondary group, **config** the control register for posedge + MSI-X-coalesce, **clear the abort mask** so the engine aborts on those causes, then **unmask** the causes — and finally lift the primary-D funnel. The third entry point, `_error_ints_unmask_one`, is the minimal one-group form used for the gen-space AXI sub-IOFICs.

### Entry Point

```text
udma_init (udma_main.c:335)                                ── ENGINE bring-up, BOUNDARY: udma-main.md
  ├─ udma_iofic_m2s_error_ints_unmask (udma_iofic.c:345)   ── PUBLIC: Tx error path
  │    ├─ iofic_config(SECONDARY, A, POSEDGE|MASK_MSI_X)    :352
  │    ├─ iofic_config(PRIMARY,   D, POSEDGE|MASK_MSI_X)    :355
  │    ├─ iofic_abort_mask_clear(SECONDARY, A, GROUP_A_ERROR_INTS)         :359
  │    ├─ iofic_abort_mask_clear(SECONDARY, C, V4_GROUP_C_M2S_ERROR_INTS)  :362
  │    ├─ udma_iofic_unmask_adv(SECONDARY, A, GROUP_A_ERROR_INTS)          :366  (validated)
  │    ├─ udma_iofic_unmask_adv(SECONDARY, C, V4_GROUP_C_M2S_ERROR_INTS)   :369  (validated)
  │    └─ udma_iofic_unmask_adv(PRIMARY,  D, D_M2S | D_2ND_IOFIC_GROUP_C)  :372  ── LIFT THE FUNNEL
  └─ udma_iofic_s2m_error_ints_unmask (udma_iofic.c:375)   ── PUBLIC: Rx mirror (grp B + grp C/S2M)

[separately, on the m2m AXI-abort path — BOUNDARY: udma-m2m.md]
  udma_m2m_set_axi_error_abort (udma_m2m.c:454)
    └─ udma_iofic_error_ints_unmask_one(&gen->iofic_base_{m2s_desc_rd..s2m_cmpl_wr}, 0xffffffff) ×6  :479-484
```

### Algorithm — the M2S error bring-up

```c
// udma_iofic_m2s_error_ints_unmask — udma_iofic.c:345.  PUBLIC.  Arm M2S/Tx errors + abort.
function udma_iofic_m2s_error_ints_unmask(udma):
    primary_grp_mask = INT_GROUP_D_M2S | INT_UDMA_V4_GROUP_D_2ND_IOFIC_GROUP_C   // :349  bit8 | bit10
    sec  = udma_iofic_reg_base_get_adv(udma, SECONDARY)
    prim = udma_iofic_reg_base_get_adv(udma, PRIMARY)

    // 1. CONFIG: posedge-latched, MSI-X-coalesced.  Causes still masked.
    iofic_config(sec,  INT_GROUP_A, POSEDGE | MASK_MSI_X)    // :352
    iofic_config(prim, INT_GROUP_D, POSEDGE | MASK_MSI_X)    // :355

    // 2. ABORT-ARM: clear abort-mask bits so the engine aborts the DMA on these causes.
    iofic_abort_mask_clear(sec, INT_GROUP_A, UDMA_IOFIC_2ND_GROUP_A_ERROR_INTS)         // :359
    iofic_abort_mask_clear(sec, INT_GROUP_C, UDMA_V4_IOFIC_2ND_GROUP_C_M2S_ERROR_INTS)  // :362  (no validate)

    // 3. UNMASK: arm delivery (validated path; group C valid only at rev>=4).
    udma_iofic_unmask_adv(udma, SECONDARY, INT_GROUP_A, UDMA_IOFIC_2ND_GROUP_A_ERROR_INTS)        // :366
    udma_iofic_unmask_adv(udma, SECONDARY, INT_GROUP_C, UDMA_V4_IOFIC_2ND_GROUP_C_M2S_ERROR_INTS) // :369

    // 4. LIFT THE FUNNEL: unmask primary D's secondary-A + secondary-C summary bits.
    udma_iofic_unmask_adv(udma, PRIMARY, INT_GROUP_D, primary_grp_mask)   // :372

// udma_iofic_s2m_error_ints_unmask — udma_iofic.c:375.  Rx mirror: SECONDARY grp B (not A),
// grp C with the S2M half, primary D = INT_GROUP_D_S2M(bit9) | ..._2ND_IOFIC_GROUP_C(bit10).  :380-403

// udma_iofic_error_ints_unmask_one — udma_iofic.c:406.  Minimal one-group form (raw block, group 0).
function udma_iofic_error_ints_unmask_one(iofic_ctrl, mask):
    iofic_config(iofic_ctrl, 0, POSEDGE | MASK_MSI_X)   // :408
    iofic_abort_mask_clear(iofic_ctrl, 0, mask)         // :409
    iofic_unmask(iofic_ctrl, 0, mask)                   // :410
    // iofic_ctrl is a raw iofic_grp_ctrl*; reinterpreted as union iofic_regs*, group 0 => the block
    // itself.  Called 6x with mask=0xffffffff for the gen-space AXI sub-IOFICs (udma_m2m.c:479-484).
```

> **QUIRK — the funnel order is config-secondary, config-primary-D, abort-arm, unmask-secondary, unmask-primary-D.** Both `config` calls happen first (secondary group, then primary D) while everything is still masked; only then are the abort masks cleared and the causes unmasked, secondary-first and primary-D **last** (`:366`–`:372`). A reimplementer who unmasks primary D before the secondary causes are armed exposes a window where D's summary bit is live but its inputs are not — harmless here (causes are level/edge-latched and the engine is not yet running copies), but the ordering is deliberate: arm the leaves, then open the funnel. The same order appears in the userspace twin ([dma/iofic §4](../dma/iofic.md#4-the-error-unmask-policy-composition)).

> **QUIRK — completion is polled via a host-memory marker, not interrupt-driven; this cell never reads a cause.** There is **no ISR, no `int_cause_grp` read, no completion-IRQ servicing** anywhere in `udma_iofic.c` or its callers on the data path. The `int_cause_grp@+0x00` and `int_status_grp@+0x20` fields are *declared* (§2) but never read in this cell. The reason is structural: the Neuron M2M copy path runs with **no hardware completion ring** — `cdesc_base == NULL` on every M2M queue, so the engine writes no completion descriptors ([udma-main §4](udma-main.md#4-consumer-primitive--completion-and-recycle)). Completion is instead observed by **polling a 4-byte host-memory marker**: the copy layer stages a final self-copy descriptor that writes a sentinel word, then busy-polls that host word until it appears ([udma-m2m](udma-m2m.md), [dma-rings](dma-rings.md)). The IOFIC's `MASK_MSI_X` programming means a fired *error* summary still becomes one MSI-X for an error/notification handler outside this cell — but the *completion* side never raises or services an interrupt. This is why primary groups B and C (the header's "RX completion" / "TX completion" groups, `:17–18`) are **never unmasked**: a polled-completion design has no use for completion IRQs. A reimplementer who wires the IOFIC into the completion path will find it never fires there; the controller exists to *abort a faulting DMA* and summarize one error MSI-X, and is configured once at `udma_init` then left off the hot path. Confidence completion is polled (absence of any cause-read consumer in this cell): **HIGH**; the marker mechanism itself is owned by the boundary cells.

> **NOTE — why poll instead of interrupt.** The rationale is the same one that drives the whole Neuron DMA path: a host↔device copy finishes in microseconds, and an interrupt round-trip (MSI-X latency + ISR dispatch + softirq) would dominate that. The driver trades a host-word poll (1 µs budget, kernel path) for the interrupt round-trip on the common, short copy, and reserves the IOFIC's one MSI-X for the rare error/abort it must *not* miss. The poll-budget and marker value (`0xabcdef01`) are owned by the [udma-m2m](udma-m2m.md) / [dma-rings](dma-rings.md) cells; this page records only that the IOFIC's completion groups are consequently dark.

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `udma_iofic_m2s_error_ints_unmask` | `udma_iofic.c:345` | **public**: Tx errors — config sec A + prim D, abort-arm sec A+C, unmask sec A+C, lift D | HIGH |
| `udma_iofic_s2m_error_ints_unmask` | `udma_iofic.c:375` | **public**: Rx mirror — sec group B + C/S2M, primary D = `D_S2M \| _2ND_IOFIC_GROUP_C` | HIGH |
| `udma_iofic_error_ints_unmask_one` | `udma_iofic.c:406` | **public**: minimal `config→abort_clear→unmask` on a raw group-0 block (6× AXI sub-IOFICs) | HIGH |

---

## Function Map (full file)

| Function | Source | Role | Confidence |
|---|---|---|---|
| `iofic_config` | `udma_iofic.c:247` | static: write `int_control_grp@+0x28 = flags` (POSEDGE\|MASK_MSI_X); still masked | HIGH |
| `iofic_unmask` | `udma_iofic.c:259` | static: write `~mask` to `int_mask_clear_grp@+0x18` (arm delivery, atomic vs auto-mask) | HIGH |
| `iofic_abort_mask_clear` | `udma_iofic.c:273` | static: write `~mask` to `int_abort_msk_grp@+0x30` (arm engine abort on causes) | HIGH |
| `udma_iofic_reg_base_get_adv` | `udma_iofic.c:288` | static: level → `&main_iofic` / `&secondary_iofic_ctrl` base | HIGH |
| `udma_iofic_level_and_group_valid` | `udma_iofic.c:307` | static: rev-gated validity (primary 0..3; secondary 0..1, +C@rev≥4) | HIGH |
| `udma_iofic_unmask_adv` | `udma_iofic.c:334` | static: validate then `iofic_unmask`; `pr_err`+skip on invalid | HIGH |
| `udma_iofic_m2s_error_ints_unmask` | `udma_iofic.c:345` | **public**: M2S/Tx error+abort bring-up; lift primary D | HIGH |
| `udma_iofic_s2m_error_ints_unmask` | `udma_iofic.c:375` | **public**: S2M/Rx mirror; lift primary D | HIGH |
| `udma_iofic_error_ints_unmask_one` | `udma_iofic.c:406` | **public**: one-group raw-block unmask (gen AXI sub-IOFICs, `mask=0xffffffff`) | HIGH |

---

## Related Components

| Name | Relationship |
|---|---|
| `iofic_config` / `iofic_unmask` / `iofic_abort_mask_clear` | the three register pokes, one MMIO write each, all over `iofic_grp_ctrl.ctrl[group]` |
| `udma_iofic_m2s/s2m_error_ints_unmask` | the policy composition `config → abort_clear → unmask → lift-D`, called from `udma_init` ([udma-main](udma-main.md)) |
| `udma_iofic_error_ints_unmask_one` | the one-group form called 6× by `udma_m2m_set_axi_error_abort` for the gen AXI sub-IOFICs ([udma-m2m](udma-m2m.md)) |
| `udma_m2m_mask_ring_id_error` | RMW-masks the ring-id prefetch cause at secondary `[0]`/`[1]` and primary C/D ([udma-m2m](udma-m2m.md)) — confirms the `0x40` stride and `secondary[0]=M2S`,`[1]=S2M` |
| `struct udma` (`gen_int_regs`, `rev_id`) | the handle this cell dereferences; mapped in `udma_handle_init_aux` ([udma-main](udma-main.md)) |
| `al_iofic_*` / `al_udma_iofic_*` (userspace) | the byte-anchored `libnrt.so` twin of this controller, agreeing on the group/level/offset model ([dma/iofic](../dma/iofic.md)) |

## Cross-References

- [UDMA Core (Annapurna/Alpine Fork)](udma-main.md) — the engine bring-up that calls `udma_iofic_m2s/s2m_error_ints_unmask` from `udma_init` (`udma_main.c:350–351`), maps the `gen_int_regs` base this cell dereferences, and owns the `cdesc_base == NULL` no-completion model that makes the completion groups dark
- [UDMA Memory-to-Memory Builder](udma-m2m.md) — the M2M builder whose completion is **polled via a host-memory marker**, not IRQ-driven (the reason this controller's completion groups are never unmasked); also the caller of `udma_iofic_error_ints_unmask_one` (6× gen AXI sub-IOFICs) and `udma_m2m_mask_ring_id_error`
- [DMA Rings and H2T Queues](dma-rings.md) — the ring container that carries the host-memory completion marker buffer and the ACK passthrough; production M2M queues carry no hardware CQ
- [IOFIC Interrupt Model](../dma/iofic.md) — the **userspace** `libnrt.so` view of the same controller (the `al_iofic_*` register pokes, byte-anchored offsets, the `~bits` inversion, the rev-keyed `0x100/0x200/0x400/0x800` secondary-summary table); treats the kernel cause catalog this page owns as opaque
- [back to index](../index.md)
