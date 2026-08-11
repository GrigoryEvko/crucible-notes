# SDMA Address Windows + APB Chain

This page closes the SoC-level address model for the SDMA engines: where each
UDMA channel (M2S / S2M / GEN / GEN_EX) and its `tdma_model` app-glue physically
sit in the Cayman (Trainium2-class, **CAYMAN = NC‑v3**) SoC address space, what
the channel **base / stride / aperture** is, and how an APB configuration write
reaches a per-channel CSR. It ties the absolute SoC bases to the *within-channel*
register offsets established by the CSR pages, so that

```
full_reg_addr = channel_base(cluster, channel) + engine_offset + register_offset
```

resolves with **no residual**. Every base/size/stride below is read directly from
the RTL-generated address map and the CSR schemas shipped in the
`cayman-arch-regs` package — these recovered artifacts are the binary-derived
ground truth, grepped and numerically verified.

> Cross-links: the channel-internal engine behaviour is in
> [the al_udma HW engine](udma-hw-engine.md); the ring/descriptor layout is in
> [the descriptor model](descriptor-model.md); the exhaustive register field
> tables are in [CSR — UDMA M2S](../control/csr/udma-m2s.md),
> [CSR — UDMA S2M](../control/csr/udma-s2m.md), and
> [CSR — UDMA_GEN + GEN_EX + TDMA_MODEL](../control/csr/udma-gen-tdma.md);
> the full SoC top-level placement is in
> [the Cayman SoC master map](../control/address/soc-master-map.md).

Primary artifacts (all under
`extracted/nested/cayman-arch-regs_tgz/output/address_map/` unless a `csrs/…`
path is given):

| artifact | what it gives |
|---|---|
| `address_map_flat.yaml` | 34 858 flat `{name, base, size, json}` nodes — absolute SoC bases |
| `address_map.vh` | LOCAL 32-bit `*_BASE` / `*_SIZE` macros (the APB-local view) |
| `apb_chain_defs.vh` | the 9 named APB daisy-chains (node order + sizes) |
| `cayman_addr_decode_neighbor.h` | the 58-bit address bit-field decode (bit 53 = PEB) |
| `csrs/sdma/{udma_m2s,udma_s2m,udma_gen,udma_gen_ex,tdma_model,cce,cme,dre}.json` | engine + app-engine register schemas |
| `csrs/fis/{fis_control,papb_bcast}.json` | the APB-path control surfaces |
| `csrs/sprot/{qos_prot,qos_host_visible,amzn_remapper,user_remapper}.json` | the protection/remap surfaces |
| `csrs/apbblk/apbblk.json` | the APB firewall (CAM allow/block) |
| `csrs/{iofabric/iofabric_model,sfabric/sfabric_model,urb/{urb,sfabric_urb}}.json` | the fabric crossbar + router-port chain heads |

Confidence tags: **HIGH** = byte-exact literal from a shipped artifact (grepped +
numerically verified); **MED** = inferred from in-file semantics + cross-file
corroboration. **OBSERVED** = read directly; **INFERRED** = reasoned;
**CARRIED** = imported from a sibling page, attributed. The Cayman address map is
the only one co-located in this view; v5/MAVERICK interiors are header/yaml-only.

---

## 1. One SDMA channel — the internal aperture

A single SDMA channel is a **0x100000 (1 MiB) stride slot** whose populated part
is a **0x80000 (512 KiB) channel container**. The layout — read verbatim from
`address_map_flat.yaml` for `APB_SE_0_SDMA_0` (container base `0x1002000000`,
container size `0x80000`) — exactly matches the within-channel engine offsets the
CSR pages established:

| off in slot | node (address-map name) | size | json schema |
|---|---|---|---|
| `+0x00000` | `…_UDMA` | `0x40000` | *(container)* |
| `+0x00000` | `…_UDMA_M2S` | `0x20000` | `csrs/sdma/udma_m2s.json` |
| `+0x20000` | `…_UDMA_S2M` | `0x18000` | `csrs/sdma/udma_s2m.json` |
| `+0x38000` | `…_UDMA_GEN` | `0x04000` | `csrs/sdma/udma_gen.json` |
| `+0x3C000` | `…_UDMA_GEN_EX` | `0x04000` | `csrs/sdma/udma_gen_ex.json` |
| `+0x40000` | `…_MISC` | `0x04000` | *(container)* |
| `+0x40000` | `…_MISC_SDMA_APP` (`tdma_model`) | `0x01000` | `csrs/sdma/tdma_model.json` |
| `+0x41000` | `…_MISC_NOTIFIC` | `0x01000` | `csrs/notific/notific_10_queue.json` |
| `+0x42000` | `…_MISC_SDMA_UG_RESERVED1` | `0x02000` | *(reserved)* |
| *(end of container `+0x80000`)* | | | |
| `+0x80000` | `…_BCAST_UDMA` *(SDMA_0 / SDMA_16 only)* | `0x40000` | *(mirror of UDMA — §3)* |

`address_map_flat.yaml:APB_SE_0_SDMA_0_UDMA_M2S` — **HIGH / OBSERVED**.

> **RECONCILIATION (vs the UDMA CSR pages).** The engine offsets
> M2S `@+0x00000` / S2M `@+0x20000` / GEN `@+0x38000` / GEN_EX `@+0x3C000` /
> `tdma_model` `@+0x40000` are confirmed as the literal child bases inside this
> `0x40000` UDMA + `0x4000` MISC channel. The window sizes match the engine
> schemas: `udma_m2s.json` `AddrWidth=17` → `0x20000`; `udma_s2m.json`
> `AddrWidth=17`, `SizeInBytes=0x18000`; `udma_gen` / `udma_gen_ex`
> `AddrWidth=14` → `0x4000`; `tdma_model` `AddrWidth=12` → `0x1000`.
> **HIGH / OBSERVED.**

> **GOTCHA — S2M does not pack against GEN.** S2M is `0x18000`, so it ends at
> `+0x38000` exactly where GEN begins. There is no slack — GEN starts immediately
> at `+0x38000` — but the channel reserves the engine windows at fixed offsets, so
> a reimplementation must place GEN at the fixed `+0x38000`, never "just after
> whatever S2M happened to consume".

### Channel stride

```
APB_SE_0_SDMA_0  @ 0x1002000000
APB_SE_0_SDMA_1  @ 0x1002100000
…
APB_SE_0_SDMA_31 @ 0x1003F00000
```

Every adjacent pair differs by **`0x100000` (1 MiB)** — the stride is uniform
across all 32 channels (verified: the set of 31 inter-channel deltas is exactly
`{0x100000}`). The container occupies only the lower `0x80000`; the upper
`0x80000` of each slot is reserved headroom, populated by `BCAST_UDMA` on the two
broadcast channels (§3). **HIGH / OBSERVED.**

---

## 2. SDMA channel base / stride / aperture — the SoC placement

Each row is byte-exact from `address_map_flat.yaml`. "SDMA region off" is the
offset of `SDMA_0` inside its enclosing region. Stride is `0x100000` everywhere.

### Die-resident SE SDMA (the `APB_SE_n` clusters)

| cluster | region base | `SDMA_0` base | SDMA region off | #ch | stride |
|---|---|---|---|---|---|
| `APB_SE_0` | `0x001000000000` | `0x001002000000` | `+0x2000000` | 32 | `0x100000` |
| `APB_SE_1` | `0x005000000000` | `0x005004000000` | `+0x4000000` | 32 | `0x100000` |
| `APB_SE_2` | `0x801000000000` | `0x801002000000` | `+0x2000000` | 32 | `0x100000` |
| `APB_SE_3` | `0x805000000000` | `0x805004000000` | `+0x4000000` | 32 | `0x100000` |

→ 4 clusters × 32 = **128 die-resident SE SDMA channels**. Each `APB_SE_n`
region is `0xC800000` (= 200 MiB). `address_map_flat.yaml:APB_SE_{0..3}` —
**HIGH / OBSERVED.**

> **QUIRK — SE-parity region offset.** `SE_0` / `SE_2` place SDMA at
> `+0x2000000`; `SE_1` / `SE_3` shift it (and the matching `USER_FIS` block) up by
> a further `0x2000000` to `+0x4000000`. Stride and internal layout are identical;
> only the in-region base differs by parity. A channel-base formula must branch on
> `cluster & 1`.

> **NOTE — the die bit.** `APB_SE_2 = APB_SE_0 | 0x800000000000`,
> `APB_SE_3 = APB_SE_1 | 0x800000000000`. Bit 47 is the **DIE** bit
> (`cayman_addr_decode_neighbor.h:CAYMAN_ADDR_DECODE_GET_DIE`, `>>47 & 1`).
> Bit 38 (`0x4000000000`) separates `SE_0` / `SE_1` within a die. Verified
> numerically: both `APB_SE_0_SDMA_0` and `APB_SE_2_SDMA_0` decode to the same
> LOCAL[46:0] `0x1002000000`, differing only in bit 47.

### IO-fabric SE SDMA (privileged PEB view; PEB = address bit 53)

| node | `SDMA_0` base | region off | #ch | stride |
|---|---|---|---|---|
| `PEB_APB_IO_0_USER_SE_0_SDMA` | `0x20008002000000` | `+0x2000000` | 32 | `0x100000` |
| `PEB_APB_IO_0_USER_SE_1_SDMA` | `0x20008004000000` | `+0x4000000` | 32 | `0x100000` |
| `PEB_APB_IO_1_USER_SE_0_SDMA` | `0x20808002000000` | `+0x2000000` | 32 | `0x100000` |
| `PEB_APB_IO_1_USER_SE_1_SDMA` | `0x20808004000000` | `+0x4000000` | 32 | `0x100000` |

→ 4 × 32 = **128 IO-fabric SE SDMA channels**. These are exposed *only* through
the **PEB aperture** (bit 53 set, `0x20000000000000`). The plain `APB_IO_n`
window (bit 53 = 0) does **not** expose `USER_SE` SDMA — verified: a flat-map
search for `APB_IO_0_USER_SE_0_SDMA_0_UDMA_M2S` returns 0 hits, while
`PEB_APB_IO_*_USER_SE_*` returns 128. The LOCAL[46:0] of the IO-fabric window is
`0x8002000000` (the APB_IO local), distinct from the die-resident `APB_SE_n`
LOCAL `0x1002000000`. `PEB_APB_IO_0/1_USER_SE_0/1_SDMA_*` — **HIGH / OBSERVED.**

> **MED / INFERRED.** Whether the die-local `APB_SE_n` and the IO-fabric
> `PEB_APB_IO USER_SE_n` windows reach two *physically distinct* engine pools or
> the *same* SE SDMA pool over two fabric routes is **not** decidable from the
> address map alone. The identical internal layout and the FIS `apb_decode`
> remap machinery (§6a) make "two fabric routes to one SE pool" the likely
> reading, but this is inference, not observation.

### Host↔device IO SDMA (the H2D / D2H engines)

| node | base | size | note |
|---|---|---|---|
| `PEB_APB_IO_0_USER_IO_SDMA_D2H` | `0x20008006800000` | `0x80000` | D2H @ `+0x6800000` |
| `PEB_APB_IO_0_USER_IO_SDMA_H2D` | `0x20008006900000` | `0x80000` | H2D @ `+0x6900000` |
| `PEB_APB_IO_1_USER_IO_SDMA_D2H` | `0x20808006800000` | `0x80000` | `\| bit47` |
| `PEB_APB_IO_1_USER_IO_SDMA_H2D` | `0x20808006900000` | `0x80000` | `\| bit47` |
| `APB_IO_0_USER_IO_SDMA_D2H` | `0x8006800000` | `0x80000` | plain BAR3 host view |
| `APB_IO_0_USER_IO_SDMA_H2D` | `0x8006900000` | `0x80000` | plain BAR3 host view |

→ 2 contexts (H2D + D2H) × 2 IO halves = **4 unicast IO SDMA channels** in the
PEB view (plus their plain `APB_IO` BAR3 alias). Each is a **full UDMA channel**
with the same `+0x00000` / `+0x20000` / `+0x38000` / `+0x3C000` / `+0x40000`
internal layout as an SE SDMA (verified on `PEB_APB_IO_0_USER_IO_SDMA_H2D`). The
H2D↔D2H stride is `0x100000`. The chain macros give the LOCAL bases
`USER_IO_SDMA_D2H_BASE = 32'h06800000`, `H2D_BASE = 32'h06900000`,
`SIZE = 32'h00080000`. **HIGH / OBSERVED.**

> **CORRECTION / clarification — there is no "d2d SDMA" UDMA engine.** D2D is a
> die-to-die **fabric** subsystem (`…_USER_FIS_IO_D2D_SUBSYS_0..7`, each with its
> own FIS) that rides the same `qos_prot` APB path as the SDMAs but binds **zero**
> `udma_m2s.json` nodes (verified: 0 `IO_D2D_SUBSYS*` nodes carry a UDMA_M2S
> schema). The only host↔device SDMA *descriptor* engines are H2D/D2H above.

### Privileged FIS + app-engine view of the SE SDMA (AMZN view)

| node | base | size | stride |
|---|---|---|---|
| `PEB_APB_IO_0_AMZN_SE_0_SDMA_0` | `0x20008010000000` | `0x80000` | `0x80000` |
| `PEB_APB_IO_0_AMZN_SE_0_SDMA_1` | `0x20008010080000` | `0x80000` | `0x80000` |

The `AMZN_SE` region sits at `+0x10000000` inside the 512 MiB PEB_APB_IO window;
32 channels per SE half, **stride `0x80000` (packed — no BCAST gap)** because the
AMZN view holds only the FIS + app-engines, *not* a UDMA window (§5C). **HIGH /
OBSERVED.**

### Aperture summary — the 280-instance ledger

`udma_m2s.json` leaf bindings (`rg -c` on the flat map, grouped by prefix):

| count | bucket |
|---|---|
| 128 | `APB_SE_0..3` SDMA (die-resident SE, unicast) |
| 128 | `PEB_APB_IO_0/1 USER_SE_0/1` (IO-fabric SE, unicast) |
| 4 | `PEB_APB_IO_0/1 USER_IO` H2D + D2H (PEB view) |
| 4 | `APB_IO_0/1 USER_IO` H2D + D2H (plain BAR3 view) |
| 16 | `BCAST_UDMA` (SDMA_0 + SDMA_16 of 8 SE-bearing clusters) |
| **280** | **total `udma_m2s.json` bindings** (`rg -c == 280`, exact) |

`tdma_model.json = 264` (264 unicast, **0** broadcast — confirmed:
`BCAST` carries no `tdma`); `udma_s2m / udma_gen / udma_gen_ex = 280` each (each
keeps its common control on the bcast channels); `cce / cme / dre = 132` each
(128 SE + 4 IO, PEB-only — §5C). **HIGH / OBSERVED.**

---

## 3. The broadcast (BCAST) aperture

`BCAST_UDMA` is a per-**channel** sub-node that exists only on **`SDMA_0` and
`SDMA_16`** of each SDMA-bearing cluster (verified: in `APB_SE_0` exactly channels
`{0, 16}` carry a `BCAST_UDMA`). It is a full M2S/S2M/GEN/GEN_EX mirror at
`+0x80000` inside that channel's 1 MiB slot:

| node | off in slot | size | json |
|---|---|---|---|
| `…_BCAST_UDMA` | `+0x80000` | `0x40000` | *(container)* |
| `…_BCAST_UDMA_M2S` | `+0x80000` | `0x20000` | `csrs/sdma/udma_m2s.json` |
| `…_BCAST_UDMA_S2M` | `+0xA0000` | `0x18000` | `csrs/sdma/udma_s2m.json` |
| `…_BCAST_UDMA_GEN` | `+0xB8000` | `0x04000` | `csrs/sdma/udma_gen.json` |
| `…_BCAST_UDMA_GEN_EX` | `+0xBC000` | `0x04000` | `csrs/sdma/udma_gen_ex.json` |

There is **no** BCAST `tdma_model` / MISC — broadcast carries only the UDMA
engine + its common control, consistent with `tdma_model`'s 264 == unicast count.
16 broadcast writers exist (2 per cluster × 8 SDMA-bearing clusters):
`SDMA_0 + SDMA_16` of `{APB_SE_0..3}` and `{PEB_APB_IO_0/1 USER_SE_0/1}`. A
broadcast M2S programs a whole group of unicast SDMAs at once; group membership is
in `tdma_model.broadcast_cfg_group` and the APB fan-out is gated by the FIS
`papb_bcast.grps.mask` (§6b). **HIGH / OBSERVED; MED for the fan-out link.**

> **GOTCHA — the container `size` field undercounts for bcast channels.** On
> `SDMA_0` / `SDMA_16` the `BCAST_UDMA` child (base `+0x80000`, size `0x40000`)
> extends to `+0xC0000` — *past* the parent container's own declared `0x80000`
> size, into the otherwise-reserved upper half of the 1 MiB slot. A downstream
> parser must trust the **child bases**, not the parent container `size`, for the
> two broadcast channels of every SDMA-bearing cluster.

---

## 4. Full-address reconciliation

The address model closes as

```
full_reg_addr = SDMA_channel_base(c, n) + engine_offset + register_offset

SDMA_channel_base(cluster c, channel n)
    = APB_SE_c region base
    + SDMA_region_off(c)            // 0x2000000 for SE_0/SE_2, 0x4000000 for SE_1/SE_3
    + n * 0x100000

engine_offset ∈ { M2S 0x00000, S2M 0x20000, GEN 0x38000, GEN_EX 0x3C000,
                  tdma_model 0x40000 }

register_offset = bundle_AddressOffset
                + reg_AddressOffset
                + array_index * BundleSizeInBytes     // for repeated bundles
```

(For the IO-fabric view, replace the region base + die bits by the
`PEB_APB_IO` window `0x8000000000 | (1<<53)`, with the same `+0x2000000` /
`n*0x100000` interior.)

### Worked examples (computed; all bases OBSERVED)

**(a) The TX doorbell — `APB_SE_0_SDMA_5`, M2S queue 5, `TDRTP_inc`.**
The M2S queue array `M2S_Q` is at bundle `@0x1000` with **`ArraySize=16`,
`BundleSizeInBytes=4096 (0x1000)`**; the per-queue **`TDRTP_inc` register is at
`+0x38`** with bitfields `reserved_31_24[31:24]` and `val[23:0]` (a 24-bit
`VAL_MASK = 0xffffff`).

```
channel-5 base = 0x1002000000 + 5*0x100000          = 0x1002500000
+ M2S engine     0x00000
+ M2S_Q[5]       0x1000 + 5*0x1000   = 0x6000
+ TDRTP_inc      0x38
= 0x1002506038      // SE_0 channel-5 queue-5 outbound-DMA doorbell
```

**(b) The RX doorbell — `RDRTP_inc`.** The S2M side is symmetric: `S2M_Q`
`@0x1000`, `ArraySize=16`, `BundleSizeInBytes=4096`, **`RDRTP_inc` at queue
`+0x38`** (abs `0x1038` for queue 0), bitfields `reserved_31_24[31:24]` /
`val[23:0]` (`VAL_MASK = 0xffffff`) — byte-identical structure to the TX
doorbell. `csrs/sdma/udma_s2m.json:S2M_Q` — **HIGH / OBSERVED.**

**(c) `APB_SE_0_SDMA_0`, `udma_gen.DMA_misc.revision`** (RO design ID):

```
0x1002000000 + GEN 0x38000 + (DMA_misc bundle 0x2100 + reg 0x04)
= 0x100203A104      // major_id[31:24]=0x1c (Cayman), minor_id[23:12]=0x360, programming_id[11:0]=0x03
```

**(d) `APB_SE_0_SDMA_0`, `tdma_model.broadcast_cfg_group`**:

```
0x1002000000 + tdma 0x40000 + (bundle 0x000 + reg 0x100)
= 0x1002040100      // the broadcast-group membership mask
```

These reconcile the within-channel offsets against the SoC bases with no residual.
**HIGH / OBSERVED + computed.**

> **NOTE (cross-page consistency).** `TDRTP_inc` (TX / M2S) and `RDRTP_inc`
> (RX / S2M) both live at queue `+0x38` (abs `0x1038` for queue 0), both
> `val[23:0]` (`VAL_MASK 0xffffff`, 24-bit), queue stride `0x1000`
> (`M2S_Q.BundleSizeInBytes = S2M_Q.BundleSizeInBytes = 4096`). These match the
> UDMA CSR pages exactly. **HIGH / OBSERVED.**

---

## 5. The three views of an SE SDMA channel

An SE SDMA channel surfaces in **three distinct address regions**, each a
different facet (all OBSERVED; sample = `SDMA_0`):

### (A) The UDMA engine — descriptor DMA + common ctrl + app-glue

| view | node | base |
|---|---|---|
| die | `APB_SE_0_SDMA_0` | `0x1002000000` (`+0x2000000` region) |
| IO  | `PEB_APB_IO_0_USER_SE_0_SDMA_0` | `0x20008002000000` (`+0x2000000`) |

Holds `UDMA_M2S/S2M/GEN/GEN_EX` + `MISC` (`tdma_model` + `notific`). `0x80000`
slot, `0x100000` stride. This is the surface the UDMA CSR pages enumerate.

### (B) Host-visible FIS — `user_remapper` + `qos_host_visible`

`APB_SE_0_USER_FIS_SDMA_0 @ 0x100C000000` (`+0xC000000` region, stride
`0x20000`). Children (offsets within the `0x10000` FIS node):

| off | child | json |
|---|---|---|
| `+0x0000` | `FIS_0_USER_ERRTRIG_TRIG_0/1` | `csrs/intc/intc_4grp_msix_unit.json` |
| `+0x2000` | `FIS_0_USER_ERRTRIG_NOTIFIC` | `csrs/notific/notific_1_queue.json` |
| `+0x4800` | `FIS_0_PAPB_BCAST` | `csrs/fis/papb_bcast.json` |
| `+0x5000` | `FIS_0_SPROT_USER_REMAPPER` | `csrs/sprot/user_remapper.json` |
| `+0x5800` | `FIS_0_SPROT_QOS` | `csrs/sprot/qos_host_visible.json` |
| `+0x10000` | `DEBUG_FIS_0_SPROT_ELA` / `QOS_PMU` / `INTERNAL_ELA` | `csrs/ela500/cxela500.json`, `csrs/sprot/qos_pmu.json` |

32 channels × `0x20000` stride. The host-side APB-path control + ELA debug.
**HIGH / OBSERVED.**

### (C) Privileged FIS + app-engines — `fis_control` + `amzn_remapper` + `qos_prot` + CME/DRE/CCE

`PEB_APB_IO_0_AMZN_SE_0_SDMA_0 @ 0x20008010000000` (`+0x10000000`, stride
`0x80000`). Children (offsets within the `0x80000` AMZN node):

| off | child | size | json |
|---|---|---|---|
| `+0x0000` | `FIS_0_CTL` | `0x2000` | `csrs/fis/fis_control.json` |
| `+0x2000` | `FIS_0_AMZN_ERRTRIG_TRIG_0/1` | `0x1000` ea | `csrs/intc/intc_4grp_no_msix_unit.json` |
| `+0x4000` | `FIS_0_AMZN_ERRTRIG_NOTIFIC` | `0x1000` | `csrs/notific/notific_1_queue.json` |
| `+0x5000` | `FIS_0_SPROT_AMZN_REMAPPER` | `0x1000` | `csrs/sprot/amzn_remapper.json` |
| `+0x6000` | `FIS_0_SPROT_QOS` | `0x1000` | `csrs/sprot/qos_prot.json` |
| `+0x10000` | `CME` | `0x1000` | `csrs/sdma/cme.json` |
| `+0x11000` | `DRE` | `0x1000` | `csrs/sdma/dre.json` |
| `+0x12000` | `DRE_ERG` | `0x40` | `csrs/erg/erg_parity_model.json` |
| `+0x13000` | `CCE` | `0x1000` | `csrs/sdma/cce.json` |
| `+0x14000` | `CCE_ERG` | `0x40` | `csrs/erg/erg_parity_model.json` |
| `+0x15000` | `SDMA_ELA_ERG` | `0x40` | `csrs/erg/erg_parity_model.json` |

This is **where the SDMA "application" data-path engines live in the address
space** — the DRE strided transpose, CCE compute/decompress, CME merge — addressed
*only* through the privileged AMZN/PEB view, alongside their protection
(`qos_prot` / `amzn_remapper`). Counts: `FIS_0_CTL` under `AMZN_SE_*_SDMA_*` =
**128**; `cme / dre / cce` = **132** each (128 SE + 4 IO H2D/D2H). **HIGH /
OBSERVED.**

> **CORROBORATION (vs the sprot/remapper CSR pages).** The privileged sprot
> placement — `FIS_0_SPROT_AMZN_REMAPPER @ +0x5000`, `FIS_0_SPROT_QOS @ +0x6000`
> — reproduces byte-exact: absolute sprot base `0x20008010005000`, remapper at
> `+0x5000`, `qos_prot` at `+0x6000`. The host-visible counterpart
> (`user_remapper` + `qos_host_visible`) sits under the `USER_FIS` region (B).
> Per-schema instance totals: `qos_prot = 1500`, `qos_host_visible = 1208`,
> `amzn_remapper = 712`, `user_remapper = 528`, consistent with the
> FIS-per-master fan-out across SDMA + IO masters. **HIGH / OBSERVED.**

---

## 6. The APB config path — how a write reaches an SDMA CSR

### 6a. The per-channel FIS (`fis_control`) — the APB ingress / decode

`fis_control.json` (`AddrWidth = 13` → `0x2000` window, `SizeInBytes = 0x2000`,
node `FIS_0_CTL`) bundles, in schema order:

```
desc(6)  axi(4)  apb(2)  apb_decode(7)  sw_cntrl(11)  iso_cntrl(5)  apb_timeout(1)
```

The APB-path-relevant registers (OBSERVED from the schema, byte offsets shown):

| bundle | reg | off | fields |
|---|---|---|---|
| `apb` `@0x28` | `gating_req` | `+0x0` | NTS-style APB gating request |
| `apb` | `outstanding_flushed` | `+0x4` | flush-complete status |
| `apb_decode` `@0x30` | `user1_base` / `user1_size` / `user1_remap` | `+0x0/4/8` | `val[31:0]` each — APB window 1 decode + remap |
| `apb_decode` | `user2_base` / `user2_size` / `user2_remap` | `+0xC/10/14` | `val[31:0]` each — APB window 2 |
| `apb_decode` | `user_fis_block_id_override` | `+0x18` | `new_block_id[9:1]`, `en[0]` |
| `apb_timeout` `@0x8C` | `ctrl` | `+0x0` | `limit[31:0]` (reset `0x2000`; `0 = Disabled`) |

`FIS_0_CTL` is literally the APB decode + remap + timeout + gating surface that
maps an incoming APB config transaction into the channel's CSR space and enforces
protection. The decode is a two-window match-and-remap:

```c
/* Per-channel APB ingress decode (fis_control.apb_decode), reconstructed from
 * csrs/fis/fis_control.json. addr is the channel-LOCAL APB offset of an
 * incoming config transaction; returns the remapped offset or signals a fault. */
typedef struct {
    uint32_t user1_base, user1_size, user1_remap;  /* apb_decode +0x0..+0x8 */
    uint32_t user2_base, user2_size, user2_remap;  /* apb_decode +0xC..+0x14 */
    uint32_t block_id_override;                     /* +0x18: new_block_id[9:1], en[0] */
    uint32_t apb_timeout_limit;                     /* apb_timeout.ctrl: limit[31:0] */
} fis_apb_decode_t;

static bool fis_apb_decode(const fis_apb_decode_t *d, uint32_t addr,
                           uint32_t *out_local, uint16_t *out_block_id)
{
    /* APB window 1: [base, base+size) -> remap. size==0 disables the window. */
    if (d->user1_size && addr >= d->user1_base &&
        addr < d->user1_base + d->user1_size) {
        *out_local = (addr - d->user1_base) + d->user1_remap;
    } else if (d->user2_size && addr >= d->user2_base &&
               addr < d->user2_base + d->user2_size) {
        *out_local = (addr - d->user2_base) + d->user2_remap;
    } else {
        return false;                       /* no window matched -> blocked / timeout */
    }
    /* Optional USER-FIS block_id override (apb chain addressing, §6c). */
    if (d->block_id_override & 0x1u)        /* en bit */
        *out_block_id = (uint16_t)((d->block_id_override >> 1) & 0x1ffu); /* [9:1] */
    return true;
}
```

`csrs/fis/fis_control.json` — **HIGH / OBSERVED** (schema + offsets);
decode control-flow **MED / INFERRED** (the field semantics are observed; the
"window 1 then window 2 then fault" precedence is the natural reading).

### 6b. `papb_bcast` — the privileged APB broadcast mask

`csrs/fis/papb_bcast.json` (`AddrWidth = 11` → `0x800`, node `FIS_0_PAPB_BCAST`
in the `USER_FIS` view at `+0x4800`). Single bundle `grps` with one register
`mask` — the broadcast-group mask that lets one APB write fan out to a group of
channels (the APB side of the `BCAST_UDMA` mechanism in §3). **HIGH / OBSERVED
schema; MED link to the bcast fan-out.**

### 6c. The APB daisy-chains — `apb_chain_defs.vh`

Nine named chains are declared, each an **ordered** node list (the physical APB
chain order) plus a matching size list. `NUM_NODES` sums to **257**:

| chain | nodes | contents (chain order) |
|---|---|---|
| `CHAIN_0` | 68 | `USER_SE_0 SDMA_0..31` \| `USER_FIS_SE_0 SDMA_0..31` \| 4 TPB |
| `CHAIN_1` | 68 | same for `SE_1` |
| `CHAIN_2` | 22 | `USER_IO SDMA D2H`(+MISC+AMZN), `H2D`(+MISC+AMZN) \| `TPB_0 FIS_0..15` |
| `CHAIN_3` | 16 | `SE_1 TPB_0 FIS_0..15` |
| `CHAIN_4` | 28 | `IO_FABRIC`, 6× `IO_TOP_SP`, `INTC_RDM`, `MISC_RAM`, HBM + HBM FIS NTS |
| `CHAIN_5` | 18 | `SE_1` HBM + HBM FIS NTS |
| `CHAIN_6` | 13 | `IO_PCIE_A` FIS + `SE_0 PCIE_S0..4` (user + amzn) + `PCIE_A` NTS |
| `CHAIN_7` | 11 | `IO_FABRIC` + `SE_1 PCIE_S0..4` (user + amzn) |
| `CHAIN_PEB` | 13 | DFX, GPIO_0/1, PLL_0/1, MISC, PCIE_M, PEB_SP, INTC, I2C, DFX_A2J, PEB FIS_0 SPROT (user + amzn) |

The chain `*_BASE` macros resolve in `address_map.vh` to **LOCAL 32-bit**
addresses, e.g.

```
`define APB_IO_0_USER_IO_SDMA_D2H_BASE   32'h06800000
`define APB_IO_0_USER_IO_SDMA_D2H_SIZE   32'h00080000
`define APB_IO_0_USER_IO_SDMA_H2D_BASE   32'h06900000
`define APB_IO_0_BASE                    32'h00000000
```

i.e. the chains are expressed in the **APB_IO LOCAL** space; the flat-map full
address adds the APB_IO window base `0x8000000000` (+ PEB bit 53 for the
privileged view): `0x8000000000 + 0x06800000 = 0x8006800000` = the flat-map
`APB_IO_0_USER_IO_SDMA_D2H` base. **HIGH / OBSERVED.**

> **KEY.** `CHAIN_0` / `CHAIN_1` are the SDMA config chains: a config write walks
> the 32 `USER_SE` SDMA engine nodes, then the 32 `USER_FIS` SDMA protection
> nodes, then the 4 TPB nodes, in that physical order. The chain-walk is a base +
> size scan:

```c
/* APB chain walk: given a chain (ordered node bases + sizes, from
 * apb_chain_defs.vh resolved against address_map.vh) and a LOCAL APB offset,
 * find the addressed node. Bases are APB_IO-local 32-bit; the caller adds the
 * APB_IO window base (0x8000000000) + PEB bit (1<<53) for the full SoC address. */
int apb_chain_select(const uint32_t *node_base, const uint32_t *node_size,
                     int num_nodes, uint32_t local_off)
{
    for (int i = 0; i < num_nodes; i++) {            /* CHAIN_0: num_nodes = 68 */
        if (local_off >= node_base[i] &&
            local_off <  node_base[i] + node_size[i])
            return i;                                /* node index in chain order */
    }
    return -1;                                       /* unmapped -> apbblk fault */
}
```

> **NOTE — APB block-id namespace.** Each chain node also carries an APB
> `block_id` (the `CAYMAN_BLOCK_ID` enum, `aws_cayman_isa_common.h`):
> `SDMA_0_0..31 = 0x000..0x01f`, `SDMA_1_0..31 = 0x020..0x03f` — the two SE
> halves. This is the namespace the FIS `user_fis_block_id_override`
> (`new_block_id[9:1]`, §6a) rewrites; the 9-bit field spans the full block-id
> range (max id observed `0x1ca`). **HIGH / OBSERVED.**

### 6d. The chain heads / fabric mux

The `*_FABRIC` regions hold the APB/AXI fabric control that routes a config write
down the addressed chain:

| region | base | contents |
|---|---|---|
| `PEB_APB_IO_0_AMZN_IO_FABRIC` | `+0x12100000` (`0x80000`) | `FIS_0_CTL`, `URB_0..9` (10 router ports, `urb.json`), `IO_FABRIC` (`iofabric_model.json`), `BLK_0`/`BLK_1` (`apbblk.json`) |
| `PEB_APB_IO_0_AMZN_SE_0_FABRIC` | `+0x12180000` (`0x80000`) | `FIS_0_CTL`, `URB_*` (`sfabric_urb.json`), `FABRIC_CTRL` (`sfabric_model.json`), `FABRIC_BLK_0/1` (`apbblk.json`) |
| `PEB_APB_IO_0_AMZN_SE_1_FABRIC` | `+0x12200000` (`0x80000`) | same shape for SE_1 |

Schema fan-out (flat-map `rg -c`): `iofabric_model = 2`, `sfabric_model = 4`
(one per AMZN_SE half × 2 PEB_IO), `urb = 136`, `sfabric_urb = 264`. The
`iofabric_model` (`AddrWidth = 8`, single bundle `iofab`, 38 regs) carries the
`axi2apb_N_{timeout,ctrl,timeout_status,timeout_type}` registers — the AXI→APB
bridge that gates a transaction onto the APB chain. The `urb.json` /
`sfabric_urb.json` router ports (`AddrWidth = 10`, bundles
`myfab / myport / spare / iofabric|sfabric_cayman / urb_rerout`) carry the
inter-fabric routing. **HIGH / OBSERVED.**

### 6e. `apbblk` — the APB firewall

`csrs/apbblk/apbblk.json` (`AddrWidth = 12` → `0x1000`). **12 instances**
(`IO_FABRIC_BLK_0/1` + `SE_0/SE_1_FABRIC_BLK_0/1`, × `APB_IO_0/1`). Bundles:

```
apbblk_ctrl(24)   apbblk_cam_ctrl(cam_ctrl_grp)   apbblk_cam(address, mask)
```

The `apbblk_ctrl` block (24 registers) is a CAM-based allow/block filter with
full telemetry: `blocklist_version`, `ctrl`, `num_cam_entries`,
`blocked_response`, `blocked_read_data`, `last_blocked_{read,write}_addr`,
`last_blocked_write_data`, and `allowed/blocked_{read,write}_count_{lo,hi}` plus
their `_shadow` copies. The CAM (`apbblk_cam`, repeated) is an `(address[31:0],
mask[31:0])` pair table. This is the enforcement point of the sprot
"block internal / host access" policy on the APB path to the SDMA (and other)
CSRs: it gates which APB addresses are permitted, counts allowed vs blocked, and
captures the last blocked transaction.

```c
/* APB firewall CAM match (csrs/apbblk/apbblk.json). One blk gates every APB
 * txn entering a fabric segment before it reaches the chain. */
bool apbblk_permitted(apbblk_t *b, uint32_t addr)
{
    for (uint32_t i = 0; i < b->ctrl.num_cam_entries; i++)
        if ((addr & b->cam[i].mask) == (b->cam[i].address & b->cam[i].mask)) {
            b->ctrl.allowed_count++;            /* allowed_{read,write}_count_* */
            return true;                        /* CAM hit = allowed */
        }
    b->ctrl.blocked_count++;                    /* blocked_{read,write}_count_* */
    b->ctrl.last_blocked_addr = addr;           /* last_blocked_{read,write}_addr */
    return false;                               /* returns ctrl.blocked_response */
}
```

`csrs/apbblk/apbblk.json` — **HIGH / OBSERVED** (bundles/registers);
match-policy **MED / INFERRED** (CAM `(address, mask)` semantics are the standard
masked-match; the allow-vs-block polarity follows the `allowed_*` / `blocked_*`
counter naming).

### Net APB topology

```
host/agent APB write
  -> APB_IO window  (BAR3 plain, or PEB bit53 privileged)
  -> fabric crossbar  (iofabric_model / sfabric_model)  +  URB router ports
  -> apbblk firewall  (CAM allow/block + telemetry)
  -> daisy-chain  (CHAIN_0/1 for SDMA: 32 USER_SE + 32 USER_FIS, in order)
  -> per-channel FIS  (fis_control apb_decode/remap + qos_prot/amzn_remapper)
  -> the UDMA_* CSR window of the addressed SDMA channel
```

The block existence and placement are **HIGH / OBSERVED**; the end-to-end
data-flow *direction* through fabric → firewall → chain → FIS → CSR is
**MED / INFERRED** from the artifact semantics.

---

## 7. Why `PEB_APB_IO` exists — the address bit decode

`cayman_addr_decode_neighbor.h` gives the authoritative 58-bit field decode (it
refines the coarse `[53:48] = CAYMAN_ID` of `cayman_addr_decode.h` into named
neighbor-routing fields):

| bits | field | mask / shift |
|---|---|---|
| `[46:0]` | `LOCAL` | `& 0x7fffffffffff` |
| `[47]` | `DIE` | `>>47 & 1` |
| `[49:48]` | `NEIGHBOR_RSVD` | `>>48 & 3` |
| `[50]` | `EXIT_SENG` | `>>50 & 1` |
| `[51]` | `EXIT_DIE` | `>>51 & 1` |
| `[52]` | `NEIGHBOR_ROUTE` | `>>52 & 1` |
| `[53]` | **`PEB`** | `>>53 & 1` |
| `[54]` | `ID_VALID` | `>>54 & 1` |
| `[55]` | `PCIE_U_RSVD` | `>>55 & 1` |
| `[56]` | `PCIE_ATTR_RELAXED_ORDERING` | `>>56 & 1` |
| `[57]` | `OK_TO_FAIL` | `>>57 & 1` |

Bit 53 = **PEB** (privileged engine block; `0x20000000000000`). The
`PEB_APB_IO_*` node prefix in the flat map is exactly this bit set. So:

* `APB_IO_0` (bit 53 = 0) = plain / host BAR3 view — exposes only `USER_IO`
  H2D/D2H SDMA.
* `PEB_APB_IO_0` (bit 53 = 1) = privileged view that *also* exposes `USER_SE`
  SDMA, the AMZN FIS + app-engines, and the PEB management blocks.

This bit is the reason the SE SDMA engines and their `qos_prot` / `amzn_remapper`
protection are reachable only through the PEB aperture. Verified numerically:
`PEB_APB_IO_0_USER_SE_0_SDMA_0 = 0x20008002000000` has bit 53 set and decodes to
LOCAL `0x8002000000`. **HIGH / OBSERVED.**

---

## 8. Cross-generation note

The only address-map artifact in this view is the Cayman (Trainium2-class,
**NC‑v3**) one (`address_map_flat.yaml`; banner "Generated for Cayman"). No
mariana/sunda `address_map` is co-located here, so cross-gen *address-placement*
differences are **not** directly observable from this page's inputs
(**HIGH / OBSERVED-absence**).

What is established cross-gen (CARRIED from the UDMA CSR pages, not re-derived
here): the SDMA *register IP* diverges across gens — `udma_gen` `revision.major_id`
is `0x1c` on Cayman (observed here in §4c) vs `0x02` on Sunda; the `tdma_model`
bundle set grows Sunda → Cayman → Mariana — while `udma_gen_ex` (the V4
virtualization window) is byte-identical across Cayman/Mariana/Mariana+/Sunda. The
within-channel **engine offsets** (M2S/S2M/GEN/GEN_EX/tdma) are the structural
constant the CSR pages treat as stable; this page confirms them for Cayman.
Whether the `0x80000` container / `0x100000` stride / `+0x2000000` vs `+0x4000000`
SE-parity region offset hold on other gens is unverifiable from the present
inputs. **MED — address side is Cayman-only.**

---

## 9. Confidence ledger

| claim | confidence | basis |
|---|---|---|
| every SDMA channel base / size / stride; `0x100000` stride; `0x80000` container | HIGH / OBSERVED | `address_map_flat.yaml` |
| the BCAST `+0x80000`→`+0xC0000` container-overrun quirk (SDMA_0/16 only) | HIGH / OBSERVED | `address_map_flat.yaml` |
| within-channel engine offsets (M2S/S2M/GEN/GEN_EX/tdma) reconciled exact | HIGH / OBSERVED | flat map + `csrs/sdma/*.json` |
| `TDRTP_inc`/`RDRTP_inc` @ queue `+0x38`, `val[23:0]`, queue stride `0x1000` | HIGH / OBSERVED | `udma_m2s.json` / `udma_s2m.json` |
| 280 M2S / 264 tdma / 280 gen+gen_ex / 132 app-engine ledger | HIGH / OBSERVED | `rg -c` on flat map |
| three-view (engine / host-FIS / priv-FIS) decomposition + sprot placement | HIGH / OBSERVED | flat map (corroborates sprot CSR pages) |
| `fis_control` apb_decode / apb_timeout / gating fields; `papb_bcast.mask` | HIGH / OBSERVED | `csrs/fis/*.json` |
| `apbblk` firewall bundles + CAM; 9 APB chains + node order (sum 257) | HIGH / OBSERVED | `apbblk.json`, `apb_chain_defs.vh` |
| iofabric/sfabric/URB chain heads; bit 53 = PEB decode | HIGH / OBSERVED | flat map, `*_fabric` jsons, decode header |
| H2D/D2H full-channel layout; D2D = fabric-not-UDMA | HIGH / OBSERVED | flat map (0 D2D udma bindings) |
| die-local vs IO-fabric SE SDMA = "two routes to one pool" | MED / INFERRED | distinct windows; same interior |
| end-to-end APB write data-flow direction; bcast fan-out link | MED / INFERRED | block semantics |
| cross-gen address placement | absence-noted | no non-Cayman map in view |

No vendor source snapshot was used; every figure above is grounded in the
RTL-generated YAML / `.vh` / CSR-schema JSON or `rg -c` on the flat map.
